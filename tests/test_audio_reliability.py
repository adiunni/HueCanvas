import asyncio
import time
import numpy as np
import pytest
from fastapi.testclient import TestClient
from app.audio import live,routing
from app.audio.bpm import detect_bpm
from tests.test_audio_beats import drum_loop


def fake_input(monkeypatch,data=None,start_error=False):
    class Stream:
        def __init__(self,**kwargs):self.callback=kwargs['callback'];self.closed=False
        def start(self):
            if start_error:raise RuntimeError('device unavailable')
            if data is not None:self.callback(data,len(data),None,None)
        def close(self):self.closed=True
    monkeypatch.setattr(live,'sd',type('SD',(),{'InputStream':Stream}))
    monkeypatch.setattr(live,'input_devices',lambda:[{'id':1,'name':'BlackHole 2ch','sample_rate':12000,'channels':2}])


async def until(predicate,timeout=1.5):
    deadline=time.monotonic()+timeout
    while not predicate():
        if time.monotonic()>deadline:raise AssertionError('Audio state did not update')
        await asyncio.sleep(.02)


def test_opposite_polarity_stereo_does_not_cancel_bpm_or_live_input(monkeypatch):
    samples=drum_loop(120)
    stereo=np.column_stack((samples,-samples)).astype('float32')
    assert detect_bpm(stereo,12000)['bpm']==pytest.approx(120,abs=1)
    fake_input(monkeypatch,stereo)
    async def scenario():
        audio=live.LiveBPM()
        try:
            await audio.start(1)
            assert audio.status()['rms']>0
            assert len(audio.status()['channel_levels'])==2
            await until(lambda:audio.current_bpm() is not None)
            assert audio.current_bpm()==pytest.approx(120,abs=1)
            assert audio.status()['capture_state']=='tracking'
        finally:await audio.stop()
    asyncio.run(scenario())


def test_right_channel_only_audio_is_not_averaged_to_half_volume(monkeypatch):
    data=np.column_stack((np.zeros(12000),np.ones(12000)*.01)).astype('float32')
    fake_input(monkeypatch,data)
    async def scenario():
        audio=live.LiveBPM()
        try:
            await audio.start(1)
            assert audio.analysis_channel==1
            assert audio.music_energy()==pytest.approx(.01)
            assert audio.status()['frames_received']==12000
        finally:await audio.stop()
        assert audio.status()['capture_state']=='idle'
    asyncio.run(scenario())


def test_silent_frames_are_reported_without_waiting_for_eight_seconds(monkeypatch):
    fake_input(monkeypatch,np.zeros((12000,2),dtype='float32'))
    async def scenario():
        audio=live.LiveBPM()
        try:
            await audio.start(1)
            assert audio.status()['capture_state']=='no_signal'
            await until(lambda:audio.error is not None)
            assert 'contain silence' in audio.error
            assert audio.current_bpm() is None and not audio.status()['warming_up']
        finally:await audio.stop()
    asyncio.run(scenario())


def test_missing_callbacks_and_processing_failure_are_distinct(monkeypatch):
    fake_input(monkeypatch)
    async def scenario():
        audio=live.LiveBPM()
        try:
            await audio.start(1)
            audio.started_at-=3
            assert audio.status()['capture_state']=='stalled'
            await until(lambda:audio.error is not None)
            assert 'not delivering frames' in audio.error
        finally:await audio.stop()
    asyncio.run(scenario())
    fake_input(monkeypatch,np.ones((12000,2),dtype='float32'))
    def fail(*args):raise RuntimeError('filter failed')
    monkeypatch.setattr(live.HitDetector,'process',fail)
    async def failed():
        audio=live.LiveBPM()
        try:
            await audio.start(1)
            assert audio.status()['capture_state']=='processing_error'
            assert 'filter failed' in audio.error
        finally:await audio.stop()
    asyncio.run(failed())


def test_open_failure_remains_visible_after_request_error(monkeypatch):
    fake_input(monkeypatch,start_error=True)
    async def scenario():
        audio=live.LiveBPM()
        with pytest.raises(ValueError,match='Cannot open'):await audio.start(1)
        assert audio.status()['capture_state']=='error'
        assert audio.error and not audio.running and audio.stream is None
    asyncio.run(scenario())


def device(id,name,uid,members=(),output=True):
    return dict(id=id,name=name,uid=uid,members=list(members),output=output)


def test_routing_identifies_actual_members_and_excludes_input_only_aggregates():
    devices=[device(1,'BlackHole 2ch','bh'),device(2,'Speakers','sp'),
             device(3,'Microphone','mic',output=False),
             device(4,'Multi-Output Device','multi',['bh','sp']),
             device(5,'Broken aggregate','bad',['bh','mic']),
             device(6,'Misleading Multi-Output','wrong',['sp'])]
    route=routing.describe_route(devices,2)
    assert not route['routed'] and route['output']=='Speakers'
    assert route['candidates']==[{'id':4,'name':'Multi-Output Device'}]
    assert routing.describe_route(devices,4)['routed']
    assert routing.describe_route(devices,1)['routed']


def test_routing_setter_rejects_unrelated_outputs(monkeypatch):
    devices=[device(1,'BlackHole 2ch','bh'),device(2,'Speakers','sp'),device(4,'Multi-Output Device','multi',['bh','sp'])]
    changed=[]
    class Core:
        def devices(self):return devices
        def default_output(self):return changed[-1] if changed else 2
        def set_output(self,id):changed.append(id)
    monkeypatch.setattr(routing.sys,'platform','darwin');monkeypatch.setattr(routing,'CoreAudio',Core)
    with pytest.raises(ValueError,match='Multi-Output'):routing.set_music_output(2)
    assert changed==[]
    assert routing.set_music_output(4)['routed'] and changed==[4]


def test_route_api_only_changes_output_on_explicit_post(monkeypatch,tmp_path):
    from app import main,config
    calls=[]
    monkeypatch.setattr(config,'MOCK_MODE',True);monkeypatch.setattr(config,'MUSIC_ENABLED',False)
    monkeypatch.setattr(config,'DATA_DIR',tmp_path)
    monkeypatch.setattr(main,'routing_status',lambda:{'routed':False,'candidates':[{'id':4,'name':'Multi-Output Device'}]})
    monkeypatch.setattr(main,'set_music_output',lambda id:calls.append(id) or {'routed':True})
    with TestClient(main.app) as client:
        assert not client.get('/api/audio/routing').json()['routed']
        assert calls==[]
        assert client.post('/api/audio/routing',json={'device_id':4}).json()['routed']
        assert calls==[4]
