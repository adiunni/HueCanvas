import asyncio
from io import BytesIO
import pytest
from PIL import Image
from fastapi.testclient import TestClient
from app.main import app
from app.palette.extract import extract_palette
from app.palette.convert import hex_to_xy
from app.palette.gamut import map_gamut
from app.hue.models import Light, Settings
from app.hue.controller import command
from app.hue.mock import MockClient
from app.animation.modes import frame
from app.animation.engine import Engine

def image_bytes(color='#743f93'):
    out=BytesIO();Image.new('RGB',(30,30),color).save(out,'PNG');return out.getvalue()
def test_palettes():
    assert extract_palette(image_bytes(),4)==['#743f93']*4
    assert extract_palette(image_bytes('#080808'),2)==['#080808']*2
    with pytest.raises(Exception): extract_palette(b'invalid')
def test_conversion_and_gamut():
    assert hex_to_xy('#000000')==[.3127,.3290]
    x,y=hex_to_xy('#ff0000');assert x> .6 and .2<y<.4
    triangle=[[0,0],[1,0],[0,1]]
    x,y=map_gamut([.9,.9],triangle);assert x+y==pytest.approx(1)
    assert map_gamut([.2,.2],triangle)==[.2,.2]
def test_capabilities_and_transitions():
    white=Light(id='w',name='white',dimming=True)
    p=command(white,200,'#ff0000',3)
    assert 'color' not in p and p['dimming']['brightness']==100
    assert p['dynamics']['duration']==3000
    assert 'dimming' not in command(Light(id='x',name='on only'),30)
    assert 'color' in command(Light(id='c',name='color',color=True),30,'#ff0000')
def test_modes():
    palette=['#ff0000','#0000ff']
    color,_=frame(Settings(mode='crossfade',speed=10),palette,5,0)
    assert color=='#800080'
    for t in range(100):
        _, factor=frame(Settings(mode='bpm'),palette,t,0)
        assert .5<=factor<=1

def test_engine_restore_and_white_commands():
    async def scenario():
        client=MockClient(); engine=Engine(client);await engine.refresh()
        await engine.start();await engine.apply(5)
        assert all('color' not in payload for id,payload in client.commands if id in ('2','3'))
        await engine.restore()
        assert all(not light.on for light in client.lights)
    asyncio.run(scenario())
def test_mock_api_upload_settings_and_controls(monkeypatch, tmp_path):
    from app import config
    monkeypatch.setattr(config, "MOCK_MODE", True)
    monkeypatch.setattr(config, "MUSIC_ENABLED", False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    with TestClient(app) as client:
        state=client.get('/api/state').json();assert state['mock'] and len(state['lights'])==4
        response=client.post('/api/artwork',files={'file':('art.png',image_bytes(),'image/png')})
        assert response.status_code==200
        state=client.get('/api/state').json();assert all(l['on'] for l in state['lights'])
        assert state['palette'][0]=='#743f93'
        assert client.get(state['artwork']).content==image_bytes()
        from app import main
        original_snapshot = main.engine.snapshot
        client.post('/api/artwork',files={'file':('next.png',image_bytes('#bf4f31'),'image/png')})
        assert main.engine.snapshot is original_snapshot
        assert all(payload['dynamics']['duration']==3000 for _,payload in main.engine.client.commands)
        assert all('color' not in payload for id,payload in main.engine.client.commands if id in ('2','3'))
        assert client.put('/api/lights/2',json={'name':'White','enabled':True,'role':'COLOR_LEFT'}).status_code==400
        assert client.put('/api/palette',json={'colors':['oops','#ff0000']}).status_code==400
        assert client.put('/api/settings',json={'transition':0}).status_code==422
        assert client.post('/api/control/pause').status_code==200
        assert client.post('/api/control/restore').status_code==200
        assert all(not l['on'] for l in client.get('/api/state').json()['lights'])
        assert client.post('/api/control/start',headers={'Origin':'https://evil.example'}).status_code==403
        assert client.get('/').status_code==200

def test_start_requires_enabled_bulbs():
    async def scenario():
        client=MockClient();engine=Engine(client);await engine.refresh()
        for light in engine.lights: light.enabled=False
        with pytest.raises(ValueError,match='No bulbs are enabled'):
            await engine.start()
        assert engine.status=='stopped' and engine.snapshot is None
        assert not client.commands
    asyncio.run(scenario())

def test_saved_selection_survives_failed_startup_discovery(monkeypatch, tmp_path):
    import json
    from app import config, main
    class FailOnceClient(MockClient):
        def __init__(self):
            super().__init__(); self.failed=False
        async def discover_lights(self):
            if not self.failed:
                self.failed=True
                raise ValueError('Temporary bridge discovery failure')
            return await super().discover_lights()
    backend=FailOnceClient()
    saved={light.id:{'name':light.name,'role':light.role,'enabled':light.id=='0'} for light in backend.lights}
    (tmp_path/'lights.json').write_text(json.dumps(saved))
    monkeypatch.setattr(config,'DATA_DIR',tmp_path)
    monkeypatch.setattr(config,'MOCK_MODE',False)
    monkeypatch.setattr(config,'MUSIC_ENABLED',False)
    monkeypatch.setattr(main,'real_client',lambda:backend)
    with TestClient(app) as client:
        assert not client.get('/api/state').json()['lights']
        assert client.post('/api/control/refresh').status_code==200
        state=client.get('/api/state').json()
        assert [l['id'] for l in state['lights'] if l['enabled']]==['0']
        assert client.post('/api/control/start').status_code==200
        assert {id for id,_ in backend.commands}=={'0'}
        assert 'color' in backend.commands[-1][1]

def test_start_failure_is_persistent_and_new_selection_is_restorable():
    async def scenario():
        backend=MockClient();engine=Engine(backend);await engine.refresh()
        for light in engine.lights: light.enabled=False
        with pytest.raises(ValueError): await engine.start()
        assert 'No bulbs are enabled' in engine.error
        engine.lights[0].enabled=True
        await engine.start()
        assert engine.error is None
        engine.lights[1].enabled=True
        await engine.start()
        assert {l.id for l in engine.snapshot}=={'0','1','2','3'}
        await engine.restore()
        assert all(not l.on for l in backend.lights)
    asyncio.run(scenario())
