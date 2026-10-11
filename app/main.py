import asyncio
import hashlib
import json
import os
import httpx
import re
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from fastapi import FastAPI, HTTPException, UploadFile, File, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from app import config
from app.hue.mock import MockClient
from app.hue.client import HueClient
from app.hue.discovery import discover, validate_ip
from app.hue.models import LightSettings, Settings
from app.animation.engine import Engine
from app.palette.extract import extract_palette
from app.music.apple_music import AppleMusic
from app.audio.live import LiveBPM, input_devices
from app.audio.routing import routing_status, set_music_output
from app.hue.entertainment import STREAM_MODES

STATIC = Path(__file__).parent/'static'
engine = None
artwork = None
palette_cache = {}
music = AppleMusic()
last_album = None
audio = LiveBPM()

def real_client(ip=None, bridge_id=None):
    key = os.getenv('HUE_APPLICATION_KEY','')
    credential = config.DATA_DIR/'credentials.json'
    saved = json.loads(credential.read_text()) if credential.exists() else {}
    if not key: key = saved.get('application_key','')
    client_key = os.getenv('HUE_CLIENT_KEY','')
    if not client_key and saved.get('application_key') == key:
        client_key = saved.get('client_key','')
    verify_tls = os.getenv('HUE_TLS_VERIFY','false').strip().lower() == 'true'
    ca_cert = os.getenv('HUE_CA_CERT','') if verify_tls else ''
    if ca_cert:
        path = Path(ca_cert).expanduser()
        ca_cert = str(path if path.is_absolute() else config.ROOT_DIR / path)
    return HueClient(ip or os.getenv('HUE_BRIDGE_IP',''),key,verify_tls=verify_tls,
                     bridge_id=(bridge_id or os.getenv('HUE_BRIDGE_ID','')) if verify_tls else '',ca_cert=ca_cert,client_key=client_key)

def save_settings():
    config.DATA_DIR.mkdir(mode=0o700,exist_ok=True)
    (config.DATA_DIR/'lights.json').write_text(json.dumps({l.id:{'name':l.name,'role':l.role,'enabled':l.enabled} for l in engine.lights}))

async def music_sync():
    global last_album, artwork
    while True:
        await asyncio.sleep(10)
        try:
            track = await music.current_track()
            if not track.get('available'): continue
            album = (track['artist'],track['album'])
            if album == last_album: continue
            key = hashlib.sha256(repr(album).encode()).hexdigest()
            path = config.DATA_DIR/f'{key}.art'
            config.DATA_DIR.mkdir(mode=0o700,exist_ok=True)
            if key not in palette_cache:
                await music.artwork(path)
                data = path.read_bytes()
                if len(data)>10*1024*1024: raise ValueError('Artwork too large')
                palette_cache[key] = await run_in_threadpool(extract_palette,data,4)
            engine.palette = palette_cache[key]
            if path.exists(): artwork = path.read_bytes()
            if engine.status == 'running': await engine.apply()
            last_album = album
        except Exception as exc:
            engine.error = 'Music artwork unavailable; upload manually. ' + str(exc)

@asynccontextmanager
async def lifespan(app):
    global engine
    engine = Engine(MockClient() if config.MOCK_MODE else real_client())
    engine.audio = audio
    try:
        saved = config.DATA_DIR/'lights.json'
        if saved.exists():
            values = json.loads(saved.read_text())
            engine.saved_settings = {key: LightSettings(**value) for key, value in values.items()}
        await engine.refresh()
    except Exception as exc: engine.error = str(exc)
    task = asyncio.create_task(engine.run())
    music_task = asyncio.create_task(music_sync()) if config.MUSIC_ENABLED else None
    yield
    for t in (task,music_task):
        if t:
            t.cancel()
            with suppress(asyncio.CancelledError): await t
    await audio.stop()
    with suppress(Exception): await engine.stop()
    await engine.client.close()

app = FastAPI(title='HueCanvas', lifespan=lifespan)
@app.middleware('http')
async def local_write_guard(request: Request, call_next):
    if request.method not in ('GET','HEAD','OPTIONS'):
        origin = request.headers.get('origin')
        if origin and origin != f'{request.url.scheme}://{request.headers.get("host")}':
            from fastapi.responses import JSONResponse
            return JSONResponse({'detail':'Cross-origin control is disabled'},status_code=403)
    return await call_next(request)

@app.exception_handler(httpx.HTTPError)
async def bridge_error(request, exc):
    from fastapi.responses import JSONResponse
    engine.error = 'Bridge request failed: ' + str(exc)
    return JSONResponse({'detail':engine.error},status_code=502)

@app.exception_handler(ValueError)
async def value_error(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse({'detail':str(exc)},status_code=400)
@app.get('/api/state')
async def state():
    return {'mock':config.MOCK_MODE,'bridge':{'ip':os.getenv('HUE_BRIDGE_IP',''),'id':getattr(engine.client,'bridge_id',''),'verify_tls':getattr(engine.client,'verify_tls',False)} if not config.MOCK_MODE else None,'lights':[l.model_dump() for l in engine.lights], 'settings':engine.settings.model_dump(),'palette':engine.palette,'status':engine.status,'error':engine.error or engine.rest_error,'artwork':('/api/artwork?v='+hashlib.sha256(artwork).hexdigest()[:16]) if artwork else None,'entertainment':True,'streaming':engine.stream is not None,'streaming_key_configured':config.MOCK_MODE or bool(getattr(engine.client,'client_key','')),'audio':audio.status(),'music_enabled':config.MUSIC_ENABLED}
@app.get('/api/artwork')
async def get_artwork():
    if artwork is None: raise HTTPException(404,'No artwork uploaded')
    return Response(artwork,media_type='image/png' if artwork.startswith(b'\x89PNG') else 'image/jpeg',headers={'Cache-Control':'private, max-age=60'})

@app.post('/api/artwork')
async def upload(file: UploadFile = File(...), count: int = Query(4,ge=2,le=6)):
    global artwork
    data = await file.read(10*1024*1024+1)
    if len(data)>10*1024*1024: raise HTTPException(413,'Artwork must be under 10 MB')
    try: colors = await run_in_threadpool(extract_palette,data,count)
    except Exception as exc: raise HTTPException(400,'Invalid JPEG or PNG artwork') from exc
    engine.palette = colors; artwork = data
    if engine.status == 'stopped': await engine.start()
    elif engine.status == 'running': await engine.apply()
    return {'palette':colors}
class Palette(BaseModel):
    colors: list[str] = Field(min_length=2,max_length=6)
@app.put('/api/palette')
async def palette(body:Palette):
    if any(not re.fullmatch(r'#[0-9a-fA-F]{6}',c) for c in body.colors): raise ValueError('Use #RRGGBB colors')
    engine.palette = body.colors
    if engine.status == 'running': await engine.apply()
    return {'ok':True}
@app.put('/api/settings')
async def settings(body:Settings):
    old_mode = engine.settings.mode
    engine.settings = body
    if engine.status == 'running':
        if (old_mode in STREAM_MODES) != (body.mode in STREAM_MODES) or (old_mode=='music_brightness') != (body.mode=='music_brightness'):
            await engine.start()
        else:
            await engine.apply()
    return {'ok':True}
@app.put('/api/lights/{light_id}')
async def light_settings(light_id:str, body:LightSettings):
    light = next((l for l in engine.lights if l.id == light_id),None)
    if not light: raise HTTPException(404,'Light not found')
    if body.role and body.role.startswith('COLOR') and not light.color: raise ValueError('This bulb has no color capability')
    if body.role and any(l.id != light_id and l.role == body.role for l in engine.lights): raise ValueError('Role already assigned; clear the other bulb first')
    for key,value in body.model_dump().items(): setattr(light,key,value)
    engine.saved_settings[light_id] = body
    save_settings()
    if engine.stream is not None: await engine.start()
    return {'ok':True}
@app.post('/api/lights/{light_id}/identify')
async def identify(light_id:str):
    light = next((l for l in engine.lights if l.id == light_id),None)
    if not light: raise HTTPException(404,'Light not found')
    async with engine.lock: await engine.client.update(light_id,{'identify':{'action':'identify'}})
    return {'ok':True}
@app.post('/api/control/{action}')
async def control(action:str):
    if action == 'start': await engine.start()
    elif action == 'pause': await engine.pause()
    elif action == 'stop': await engine.stop()
    elif action == 'restore': await engine.restore()
    elif action == 'refresh': await engine.refresh()
    else: raise HTTPException(404,'Unknown action')
    return {'ok':True}
@app.get('/api/audio/devices')
async def audio_devices():
    return {'devices':await run_in_threadpool(input_devices)}
@app.get('/api/audio/routing')
async def audio_routing():
    return await run_in_threadpool(routing_status)
class OutputRoute(BaseModel):
    device_id: int = Field(ge=1)
@app.post('/api/audio/routing')
async def route_audio(body:OutputRoute):
    return await run_in_threadpool(set_music_output,body.device_id)
class AudioInput(BaseModel):
    device_id: int = Field(ge=0)
@app.post('/api/audio/start')
async def start_audio(body:AudioInput):
    if engine.status=='running' and (engine.settings.bpm_source=='audio' or engine.uses_audio_hits()):
        await engine.pause()
    await audio.start(body.device_id)
    return audio.status()
@app.post('/api/audio/stop')
async def stop_audio():
    if engine.settings.mode in (*STREAM_MODES,'music_brightness') and (engine.settings.bpm_source == 'audio' or engine.uses_audio_hits()):
        await engine.pause()
    await audio.stop()
    return audio.status()
@app.get('/api/bridges')
async def bridges(): return await discover()
class Pair(BaseModel):
    ip: str
    bridge_id: str = Field(default='',pattern=r'^(?:[a-fA-F0-9]{16})?$')
@app.post('/api/pair')
async def pair(body:Pair):
    if config.MOCK_MODE: raise ValueError('Set MOCK_MODE=false and restart before pairing hardware')
    validate_ip(body.ip)
    client = real_client(body.ip,body.bridge_id)
    try:
        key = await client.pair()
        config.DATA_DIR.mkdir(mode=0o700,exist_ok=True)
        path = config.DATA_DIR/'credentials.json'
        fd = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as file: json.dump({'application_key':key},file)
        os.chmod(path,0o600)
        await engine.stop()
        async with engine.lock:
            engine.status = 'stopped'; engine.snapshot = None
            await engine.client.close(); engine.client = client
        await engine.refresh()
    except Exception:
        if engine.client is not client: await client.close()
        raise
    return {'ok':True,'message':'Paired; configure bulb roles before starting'}
@app.get('/api/music')
async def current_music():
    if not config.MUSIC_ENABLED: return {'available':False,'reason':'Enable APPLE_MUSIC_ENABLED in .env to use Music scripting'}
    return await music.current_track()
@app.get('/')
async def index(): return FileResponse(STATIC/'index.html')
app.mount('/static',StaticFiles(directory=STATIC),name='static')
