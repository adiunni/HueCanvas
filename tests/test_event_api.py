import asyncio
import shutil
import subprocess
from pathlib import Path
import pytest
from io import BytesIO
import numpy as np
from scipy.io import wavfile
from fastapi.testclient import TestClient
from app import config, main
from app.events.models import Cue, TransportUpdate
from app.events.store import CueStore


def wav():
    t=np.arange(12000*8)/12000
    signal=(.03*np.sin(2*np.pi*200*t)).astype(np.float32)
    signal[t>6]=0
    out=BytesIO();wavfile.write(out,12000,signal)
    return out.getvalue()


def client_setup(monkeypatch,tmp_path):
    monkeypatch.setattr(config,'MOCK_MODE',True)
    monkeypatch.setattr(config,'MUSIC_ENABLED',False)
    monkeypatch.setattr(config,'DATA_DIR',tmp_path)


def test_api_manual_edit_preview_audition_and_cache_without_capture(monkeypatch,tmp_path):
    client_setup(monkeypatch,tmp_path)
    with TestClient(main.app) as client:
        assert not client.get('/api/state').json()['audio']['running']
        options=client.get('/api/events').json()['options']
        assert not options['enabled'] and options['reduced_motion']
        cue={'track_id':'authorized-test','type':'FINAL_IMPACT','position':4}
        created=client.put('/api/events/cue',json=cue).json()
        assert created['user_edited'] and created['override']
        preview=client.post('/api/events/preview',json=created).json()
        assert len(preview['samples'])==101
        assert main.engine.status=='stopped' and not main.engine.client.commands
        assert client.post('/api/events/audition',json=created).status_code==400
        assert client.put('/api/events/options',json={**options,'enabled':True,'output':'entertainment'}).status_code==200
        assert client.put('/api/events/transport',json={'track_id':cue['track_id'],'position':0}).status_code==200
        assert client.post('/api/control/start').status_code==200
        assert client.post('/api/events/audition',json=created).status_code==200
        assert main.engine.events.history[-1]['preview']
        assert main.engine.stream and not main.audio.running
        result=client.post('/api/events/analyze?track_id=authorized-test',files={'file':('authorized.wav',wav(),'audio/wav')})
        assert result.status_code==200,result.text
        assert not result.json()['cached']
        result=client.post('/api/events/analyze?track_id=authorized-test',files={'file':('authorized.wav',wav(),'audio/wav')})
        assert result.json()['cached']
        data=client.get('/api/events?track_id=authorized-test').json()
        assert next(c for c in data['events'] if c['id']==created['id'])['position']==4
        assert 'features' not in data['analysis']
        assert list((tmp_path/'events').glob('*.features.json'))
        assert not list(tmp_path.rglob('*.wav'))
        assert not main.audio.running
        assert client.post('/api/control/restore').status_code==200
        assert main.engine.stream is None and not main.engine.white_task
        assert all(not l.on for l in main.engine.client.lights)
        assert 'events.js' in client.get('/').text


def test_api_candidate_review_validation_and_wrong_track_does_not_schedule(monkeypatch,tmp_path):
    client_setup(monkeypatch,tmp_path)
    with TestClient(main.app) as client:
        candidate=Cue(track_id='test',type='DROP',position=1,source='offline',review='pending',confidence=.8)
        store=CueStore(tmp_path/'events')
        store.merge_analysis('test',[candidate],{})
        client.put('/api/events/transport',json={'track_id':'test','position':0,'playing':True})
        clock=main.engine.events.clock
        assert not clock.poll(main.engine.events.cues,clock.anchor+1)
        accepted={**candidate.model_dump(mode='json'),'review':'accepted'}
        assert client.put('/api/events/cue',json=accepted).status_code==200
        client.put('/api/events/transport',json={'track_id':'test','position':0,'playing':True,'seek':True})
        assert len(clock.poll(main.engine.events.cues,clock.anchor+1))==1
        assert not clock.poll(main.engine.events.cues,clock.anchor+1.1)
        assert client.put('/api/events/options',json={'confidence':1.1}).status_code==422
        assert client.put('/api/events/transport',json={'track_id':'x','position':0,'loop_end':2}).status_code==422
        assert client.post('/api/events/analyze?track_id=test',files={'file':('bad.wav',b'bad','audio/wav')}).status_code==400
        assert client.put('/api/events/cue',json=accepted,headers={'Origin':'https://example.com'}).status_code==403


def test_edit_during_analysis_is_not_overwritten(monkeypatch,tmp_path):
    from app.events import api
    client_setup(monkeypatch,tmp_path)
    def analyze(data,track_id):
        edited=Cue(track_id=track_id,id='manual',type='FINAL_IMPACT',position=6)
        CueStore(tmp_path/'events').put(edited)
        return [Cue(track_id=track_id,type='FINAL_IMPACT',position=6.2,source='offline')],{'algorithm_version':'test'}
    monkeypatch.setattr(api,'analyze_wav',analyze)
    with TestClient(main.app) as client:
        assert client.post('/api/events/analyze?track_id=test',files={'file':('x.wav',b'fixture')}).status_code==200
        cues=client.get('/api/events?track_id=test').json()['events']
        assert len(cues)==1 and cues[0]['id']=='manual' and cues[0]['position']==6


def test_music_position_pause_resume_and_track_change(monkeypatch,tmp_path):
    from app.animation.engine import Engine
    from app.hue.mock import MockClient
    from app.music.apple_music import AppleMusic
    from app.music import apple_music
    client_setup(monkeypatch,tmp_path)
    async def scenario():
        engine=Engine(MockClient());monkeypatch.setattr(main,'engine',engine)
        engine.events.options.enabled=True;engine.events.options.follow_music=True
        await engine.start()
        track={'available':True,'id':'music','position':4,'playing':False}
        await main.music_transport(track)
        assert engine.status=='paused' and engine.events.follow_paused
        await main.music_transport({**track,'playing':True})
        assert engine.status=='running'
        await main.music_transport({**track,'id':'next','position':0,'playing':True})
        assert engine.events.clock.transport.track_id=='next' and not engine.events.active
        await engine.pause()
        await main.music_transport({**track,'playing':True})
        assert engine.status=='paused'  # explicit Pause cannot be undone by polling
        await engine.stop()
        async def script(*a,**kw):return 'id\x1fTitle\x1fArtist\x1fAlbum\x1fpaused\x1f12.5\x1f310.2'
        monkeypatch.setattr(apple_music,'run_script',script)
        monkeypatch.setattr(apple_music.sys,'platform','darwin')
        result=await AppleMusic().current_track()
        assert result['position']==12.5 and not result['playing']
    asyncio.run(scenario())


def test_dashboard_event_handlers():
    node=shutil.which('node')
    if not node:pytest.skip('Node is needed for dashboard-handler verification')
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run([node,'tests/events_browser.cjs'],cwd=root,capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr
