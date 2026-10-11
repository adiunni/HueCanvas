import asyncio
import hashlib
import json
import os
import httpx
import re
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from fastapi import FastAPI, HTTPException, UploadFile, File, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from typing import Literal
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from app import config
from app.hue.mock import MockClient
from app.hue.client import HueClient
from app.hue.discovery import discover, validate_ip
from app.hue.models import LightSettings, Settings
from app.animation.engine import Engine
from app.animation.beats import palette_color
from app.palette.extract import extract_palette
from app.music.apple_music import AppleMusic
from app.music.sync import ArtworkSync
from app.audio.live import LiveBPM, input_devices
from app.audio.routing import routing_status, set_music_output
from app.hue.entertainment import STREAM_MODES

STATIC = Path(__file__).parent/'static'
engine = None
artwork = None
music_track = None
music_error = None
music_revision = 0
music_subscribers = set()
music = AppleMusic()
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

def artwork_url():
    return '/api/artwork?v='+hashlib.sha256(artwork).hexdigest()[:16] if artwork else None


def music_payload():
    return {'revision':music_revision,'track':music_track,'artwork':artwork_url(),
            'palette':engine.palette if engine else None,'error':music_error}


def publish_music():
    global music_revision
    music_revision+=1
    payload=music_payload()
    for queue in tuple(music_subscribers):
        if queue.full():queue.get_nowait()
        queue.put_nowait(payload)


async def music_update(kind,value):
    global music_track,music_error,artwork
    if kind=='track':
        music_track=value;artwork=None;music_error=None
        if audio.running and hasattr(audio,'track_changed'):audio.track_changed()
    elif kind=='artwork':artwork=value;music_error=None
    elif kind=='palette':engine.palette=value;music_error=None
    elif kind=='error':music_error='Music artwork unavailable; upload manually. '+value
    publish_music()
    if kind=='palette' and engine.status=='running':
        try:await engine.apply(only_if_running=True)
        except Exception as exc:engine.error=str(exc)


async def music_sync():
    sync=ArtworkSync(music,config.DATA_DIR,music_update,palette_size=lambda:engine.settings.palette_size)
    await sync.run()

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
    return {'mock':config.MOCK_MODE,'bridge':{'ip':os.getenv('HUE_BRIDGE_IP',''),'id':getattr(engine.client,'bridge_id',''),'verify_tls':getattr(engine.client,'verify_tls',False)} if not config.MOCK_MODE else None,'lights':[l.model_dump() for l in engine.lights], 'settings':engine.settings.model_dump(),'palette':engine.palette,'status':engine.status,'playback_message':engine.playback_message,'error':engine.error or engine.rest_error,'artwork':artwork_url(),'music_track':music_track,'music_error':music_error,'music_revision':music_revision,'entertainment':True,'streaming':engine.stream is not None,'streaming_key_configured':config.MOCK_MODE or bool(getattr(engine.client,'client_key','')),'audio':audio.status(),'music_enabled':config.MUSIC_ENABLED}
@app.get('/api/music/events')
async def music_events(request:Request):
    async def events():
        queue=asyncio.Queue(maxsize=1);music_subscribers.add(queue)
        try:
            yield 'data: '+json.dumps(music_payload())+'\n\n'
            while not await request.is_disconnected():
                try:payload=await asyncio.wait_for(queue.get(),timeout=15)
                except asyncio.TimeoutError:
                    yield ': heartbeat\n\n'
                    continue
                yield 'data: '+json.dumps(payload)+'\n\n'
        finally:music_subscribers.discard(queue)
    return StreamingResponse(events(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

@app.get('/api/artwork')
async def get_artwork():
    if artwork is None: raise HTTPException(404,'No artwork uploaded')
    return Response(artwork,media_type='image/png' if artwork.startswith(b'\x89PNG') else 'image/jpeg',headers={'Cache-Control':'private, max-age=60'})

@app.post('/api/artwork')
async def upload(file: UploadFile = File(...), count: int | None = Query(None,ge=2,le=6)):
    global artwork
    data = await file.read(10*1024*1024+1)
    if len(data)>10*1024*1024: raise HTTPException(413,'Artwork must be under 10 MB')
    if count: engine.settings = engine.settings.model_copy(update={'palette_size':count})
    try: colors = await run_in_threadpool(extract_palette,data,engine.settings.palette_size)
    except Exception as exc: raise HTTPException(400,'Invalid JPEG or PNG artwork') from exc
    engine.palette = colors; artwork = data
    if engine.status == 'stopped': await engine.start()
    elif engine.status == 'running': await engine.apply(only_if_running=True)
    return {'palette':colors}
class Palette(BaseModel):
    colors: list[str] = Field(min_length=2,max_length=6)
@app.put('/api/palette')
async def palette(body:Palette):
    if any(not re.fullmatch(r'#[0-9a-fA-F]{6}',c) for c in body.colors): raise ValueError('Use #RRGGBB colors')
    engine.palette = body.colors
    if engine.status == 'running': await engine.apply(only_if_running=True)
    return {'ok':True}
async def resize_palette(size):
    # Re-cluster the current artwork; without artwork, keep the edited swatches
    # and blend between them to add colors.
    old=list(engine.palette)
    if artwork is not None:
        try: colors=await run_in_threadpool(extract_palette,artwork,size)
        except Exception as exc: raise HTTPException(400,'Cannot re-extract the artwork palette') from exc
    elif size<=len(old): colors=old[:size]
    else: colors=[palette_color(old,i*len(old)/size) for i in range(size)]
    engine.palette=colors;publish_music()
@app.put('/api/settings')
async def settings(body:Settings):
    old_settings = engine.settings
    old_mode = old_settings.mode
    engine.settings = body
    if hasattr(audio,'percussion_sensitivity'): audio.percussion_sensitivity=body.flash_sensitivity
    if hasattr(audio,'drum_isolation'): audio.drum_isolation=body.drum_isolation=='stems'
    if hasattr(audio,'set_frequency_thresholds'):
        audio.set_frequency_thresholds(body.bass_threshold_db,body.bass_fallback_db,body.mids_threshold_db)
    if old_mode!=body.mode or old_settings.flash_trigger!=body.flash_trigger or any(getattr(old_settings,key)!=getattr(body,key) for key in ('bass_threshold_db','bass_fallback_db','mids_threshold_db','flash_sensitivity','roll_max_hz','drum_isolation')):
        engine.hit_flashes.reset(getattr(audio,'hit_counter',0))
    if old_settings.palette_size!=body.palette_size:
        await resize_palette(body.palette_size)
    if engine.status == 'running':
        if (old_mode in STREAM_MODES) != (body.mode in STREAM_MODES) or (old_mode=='music_brightness') != (body.mode=='music_brightness'):
            await engine.start()
        else:
            await engine.apply(only_if_running=True)
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
def needs_audio(settings):
    # Ambient looks never open capture by themselves; their white lamps follow
    # music only when listening is already on.
    return settings.mode in STREAM_MODES or settings.mode=='music_brightness'

async def choose_input(device_id=None):
    """The requested input, else the last one used, else a BlackHole loopback."""
    if device_id is not None: return device_id
    if getattr(audio,'device_id',None) is not None: return audio.device_id
    devices=await run_in_threadpool(input_devices)
    loopback=next((d for d in devices if d['loopback']),None)
    if loopback is None:
        raise ValueError('No BlackHole input found. Open Audio setup to choose an input and route Mac sound through it.')
    return loopback['id']

async def listen(device_id=None):
    if hasattr(audio,'set_frequency_thresholds'):
        audio.set_frequency_thresholds(engine.settings.bass_threshold_db,engine.settings.bass_fallback_db,engine.settings.mids_threshold_db)
    if hasattr(audio,'percussion_sensitivity'): audio.percussion_sensitivity=engine.settings.flash_sensitivity
    if hasattr(audio,'drum_isolation'): audio.drum_isolation=engine.settings.drum_isolation=='stems'
    await audio.start(await choose_input(device_id))

@app.post('/api/control/{action}')
async def control(action:str):
    if action == 'start':
        # Pressing Start on a music-reactive look starts listening too; capture
        # still begins only on this explicit action.
        if needs_audio(engine.settings) and not audio.running: await listen()
        await engine.start()
    elif action == 'pause': await engine.pause()
    elif action == 'stop': await engine.stop()
    elif action == 'restore':
        await engine.restore()
        if audio.running: await audio.stop()
    elif action == 'refresh': await engine.refresh()
    else: raise HTTPException(404,'Unknown action')
    return {'ok':True}
@app.get('/api/audio/devices')
async def audio_devices():
    return {'devices':await run_in_threadpool(input_devices)}
@app.get('/api/audio/spectrum')
async def audio_spectrum():
    return await run_in_threadpool(audio.spectrum)
@app.get('/api/audio/routing')
async def audio_routing():
    return await run_in_threadpool(routing_status)
class OutputRoute(BaseModel):
    device_id: int = Field(ge=1)
@app.post('/api/audio/routing')
async def route_audio(body:OutputRoute):
    return await run_in_threadpool(set_music_output,body.device_id)
class AudioInput(BaseModel):
    device_id: int | None = Field(default=None, ge=0)
@app.post('/api/audio/start')
async def start_audio(body:AudioInput):
    if engine.status in ('running','waiting_audio') and (engine.settings.bpm_source=='audio' or engine.uses_audio_hits() or engine.white_music_active()):
        await engine.pause()
    await listen(body.device_id)
    return audio.status()
class TempoScale(BaseModel):
    factor: Literal[0.5, 2]
@app.post('/api/audio/tempo')
async def scale_tempo(body:TempoScale):
    audio.scale_tempo(body.factor)
    return audio.status()
@app.post('/api/audio/stop')
async def stop_audio():
    if engine.status in ('running','waiting_audio') and (engine.settings.bpm_source == 'audio' or engine.uses_audio_hits() or engine.white_music_active()):
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
