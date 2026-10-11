import asyncio
import struct
import time
import threading
import numpy as np
import pytest
from fastapi.testclient import TestClient
from app.audio.bpm import detect_bpm
from app.animation.beats import pulse_factor, effective_division
from app.animation.engine import Engine
from app.hue.models import Light, Settings
from app.hue.mock import MockClient
from app.hue.entertainment import encode_frame, select_area, EntertainmentSession


def test_live_capture_opens_on_main_thread_and_releases_memory(monkeypatch):
    from app.audio import live
    main_thread = threading.get_ident()
    opened = []
    class Stream:
        def __init__(self, **kwargs):
            assert threading.get_ident() == main_thread
            self.callback = kwargs['callback']; self.closed = False
            opened.append(self)
        def start(self):
            self.callback(np.ones((4800, 2), dtype=np.float32)*.1, 4800, None, None)
        def close(self):
            assert threading.get_ident() == main_thread
            self.closed = True
    monkeypatch.setattr(live, 'sd', type('SD', (), {'RawInputStream': Stream}))
    monkeypatch.setattr(live, 'input_devices', lambda: [{'id':1,'name':'BlackHole 2ch','sample_rate':48000,'channels':2}])
    async def scenario():
        audio = live.LiveBPM()
        await audio.start(1)
        assert audio.running and audio.frames == 4800 and audio.rms > 0
        await audio.start(1)
        assert opened[0].closed and len(opened) == 2
        await audio.stop()
        assert opened[1].closed and not audio.running
        assert not audio.buffer and audio.frames == 0 and audio.rms == 0
    asyncio.run(scenario())


def drum_loop(bpm, sr=12000, seconds=18):
    samples=np.zeros(sr*seconds)
    for beat in np.arange(.2,seconds,60/bpm):
        start=int(beat*sr);length=min(int(.015*sr),len(samples)-start)
        t=np.arange(length)/sr
        samples[start:start+length]+=np.sin(2*np.pi*100*t)*np.exp(-t*150)
    return samples

@pytest.mark.parametrize('tempo',[60,80,100,120,140,160,180])
def test_estimates_known_tempos_and_phase(tempo):
    result=detect_bpm(drum_loop(tempo),12000)
    assert result and abs(result['bpm']-tempo)<1
    assert result['confidence']>.5
    assert 17 < result['last_beat_seconds'] < 18
    phase_error=((result['last_beat_seconds']-.2+30/tempo)%(60/tempo))-30/tempo
    assert abs(phase_error)<.05

def test_audio_silence_noise_and_sample_rates():
    assert detect_bpm(np.zeros(12000*18),12000) is None
    assert detect_bpm(np.ones(12000*4),12000) is None
    noise=np.random.default_rng(123).normal(0,.05,12000*18)
    assert detect_bpm(noise,12000) is None
    result=detect_bpm(drum_loop(120,sr=48000),48000)
    assert abs(result['bpm']-120)<1
    stopped=drum_loop(120);stopped[-12000:]=0
    assert detect_bpm(stopped,12000) is None

def test_pulse_modes_and_flash_rate():
    assert pulse_factor('beat_flash',120,0,0,low=.2)==1
    assert pulse_factor('beat_flash',120,.25,0,low=.2)==.2
    assert pulse_factor('beat_gentle',120,0,0,low=.2)==pytest.approx(1)
    assert pulse_factor('beat_gentle',120,.25,0,low=.2)==pytest.approx(.2)
    assert pulse_factor('beat_gentle',120,.125,0,low=.2)==pytest.approx(.6)
    assert effective_division(190,1)==2
    assert effective_division(120,1)==1
    for bpm in range(55,191): assert bpm/(60*effective_division(bpm,1))<=3

def test_stream_frame_encoding():
    area='00000000-0000-0000-0000-000000000001'
    frame=encode_frame(area,257,{1:(.5,.25,.75),0:(0,1,1)})
    assert len(frame)==66
    assert frame[:16]==b'HueStream'+bytes([2,0,1,0,0,1,0])
    assert frame[16:52].decode()==area
    assert struct.unpack('!BHHH',frame[52:59])==(0,0,65535,65535)
    assert struct.unpack('!BHHH',frame[59:66])==(1,32768,16384,49151)

def area_resources():
    lights=[Light(id='c1',name='left',owner='d1',color=True,enabled=True),Light(id='c2',name='right',owner='d2',color=True,enabled=True),Light(id='w',name='white',owner='dw',enabled=True)]
    services=[{'id':'e1','owner':{'rid':'d1'}},{'id':'e2','owner':{'rid':'d2'}}]
    area={'id':'00000000-0000-0000-0000-000000000001','status':'inactive','channels':[{'channel_id':0,'members':[{'service':{'rid':'e1'}}]},{'channel_id':1,'members':[{'service':{'rid':'e2'}}]}]}
    return lights,services,area

def test_area_selection_excludes_white_and_unselected_devices():
    lights,services,area=area_resources()
    assert select_area([area],services,lights)==(area['id'],{0:'c1',1:'c2'})
    lights[1].enabled=False
    with pytest.raises(ValueError,match='exactly'): select_area([area],services,lights)
    lights[1].enabled=True;area['status']='active'
    with pytest.raises(ValueError,match='already active'): select_area([area],services,lights)

def test_mock_beat_frames_keep_white_fill_on_rest_and_restore():
    async def scenario():
        client=MockClient();engine=Engine(client);await engine.refresh()
        engine.settings=Settings(mode='beat_flash',bpm=120,flash_low=20,bpm_source='manual')
        await engine.start()
        stream=engine.stream
        assert stream and stream.mock
        initial_rest=len(client.commands)
        assert initial_rest==2
        assert all(id in ('2','3') and 'color' not in payload for id,payload in client.commands)
        engine.started=time.monotonic()-.25
        await engine.apply();await engine.apply()
        assert len(client.commands)==initial_rest
        assert client.lights[0].brightness==pytest.approx(12)
        assert len(stream.frames)==3
        await engine.pause();assert engine.stream is None and engine.status=='paused'
        await engine.restore();assert all(not light.on for light in client.lights)
    asyncio.run(scenario())

def test_stream_failure_releases_entertainment_area():
    lights,services,area=area_resources()
    class Client:
        ip='192.168.1.2';client_key='a'*32
        http=type('HTTP',(),{'headers':{'hue-application-key':'test-key'}})()
        def __init__(self): self.actions=[]
        async def request(self,method,path,**kwargs):
            if path.endswith('/entertainment_configuration'): return {'data':[area]}
            if path.endswith('/entertainment'): return {'data':services}
            self.actions.append(kwargs['json']['action']);return {'data':[]}
        async def update(self,id,payload): pass
    class Transport:
        def connect(self,*args): raise ValueError('DTLS failed')
        def close(self): pass
    async def scenario():
        client=Client();session=EntertainmentSession(client,Transport)
        with pytest.raises(ValueError,match='DTLS failed'): await session.start(lights)
        assert client.actions==['start','stop']
        assert session.transport is None and not session.activated
    asyncio.run(scenario())

def test_audio_api_and_stop_terminates_beat_effect(monkeypatch,tmp_path):
    from app import config, main
    class Audio:
        def __init__(self): self.running=False;self.phase_at=time.monotonic()
        def current_bpm(self): return 120 if self.running else None
        def status(self): return {'running':self.running,'bpm':self.current_bpm()}
        async def start(self,id): assert id==1;self.running=True
        async def stop(self): self.running=False
    monkeypatch.setattr(config,'MOCK_MODE',True);monkeypatch.setattr(config,'MUSIC_ENABLED',False);monkeypatch.setattr(config,'DATA_DIR',tmp_path)
    monkeypatch.setattr(main,'audio',Audio())
    devices=[]
    monkeypatch.setattr(main,'input_devices',lambda:devices)
    with TestClient(main.app) as client:
        assert client.put('/api/settings',json={'mode':'beat_gentle','bpm_source':'audio'}).status_code==200
        # Start listens by itself, but only on a loopback input.
        response=client.post('/api/control/start')
        assert response.status_code==400 and 'BlackHole' in response.json()['detail'] and not main.audio.running
        devices.append({'id':1,'name':'BlackHole 2ch','loopback':True})
        assert client.get('/api/audio/devices').json()['devices'][0]['loopback']
        assert client.post('/api/control/start').status_code==200 and main.audio.running
        assert client.get('/api/state').json()['streaming']
        stream = main.engine.stream
        rest_count = len(main.engine.client.commands)
        assert client.put('/api/settings',json={'mode':'beat_flash','bpm_source':'audio','brightness':80,'flash_low':10}).status_code==200
        assert main.engine.stream is stream
        assert len(main.engine.client.commands) == rest_count
        assert client.post('/api/audio/stop').status_code==200
        state=client.get('/api/state').json()
        assert not state['streaming'] and state['status']=='paused'
