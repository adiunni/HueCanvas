import asyncio
import numpy as np
import pytest
from fastapi.testclient import TestClient
from app.animation.engine import Engine
from app.animation.music_brightness import grouped_beat_factor, energy_factor
from app.hue.models import Settings
from app.hue.mock import MockClient
from app.audio.bpm import align_beat_phase


def test_grouped_beats_avoid_aliasing_fast_music_to_a_steady_rest_target():
    assert grouped_beat_factor(120,0,0)==1
    assert grouped_beat_factor(120,0,2.01)==.2
    assert grouped_beat_factor(120,0,4.01)==1
    assert grouped_beat_factor(240,0,2.01)==.2
    assert grouped_beat_factor(60,0,4.01)==.2
    for bpm in [30,60,100,120,180,240]:
        times=np.arange(0,30,.01)
        values=[grouped_beat_factor(bpm,0,t) for t in times]
        changes=times[1:][np.diff(values)!=0]
        assert not len(changes) or min(np.diff(np.r_[0,changes]))>=2-.01


def test_energy_fallback_is_bounded_and_responds_to_volume():
    assert energy_factor(0)==.2
    assert .2<energy_factor(.008)<energy_factor(.04)<1
    assert energy_factor(.08)==1
    assert energy_factor(1)==1


def test_bpm_remeasurement_preserves_progress_of_grouped_beats():
    anchor=None;factors=[]
    for at in [100,102,104,106,108]:
        anchor=align_beat_phase(anchor,at,120)
        factors.append(grouped_beat_factor(120,anchor,at+.1))
    assert factors==[1,.2,1,.2,1]
    assert align_beat_phase(100,108.02,120)==pytest.approx(100.02)


def test_live_monitor_keeps_existing_phase_anchor(monkeypatch):
    from app.audio import live
    original_sleep=asyncio.sleep
    async def scenario():
        audio=live.LiveBPM();audio.running=True;audio.bpm=120;audio.phase_at=100
        audio.sample_rate=12000;audio.frames=12000*10
        audio.buffer.append(np.ones(audio.frames)*.1)
        audio.end_time=110
        calls=0
        async def tick(seconds):
            nonlocal calls
            calls+=1
            if calls>1:raise asyncio.CancelledError
            await original_sleep(0)
        monkeypatch.setattr(live.asyncio,'sleep',tick)
        monkeypatch.setattr(live.time,'monotonic',lambda:110)
        monkeypatch.setattr(live,'detect_bpm',lambda *a:{'bpm':120,'confidence':.9,'duration':10,'last_beat_seconds':9.5})
        with pytest.raises(asyncio.CancelledError):await audio._monitor()
        assert audio.phase_at==100
        assert grouped_beat_factor(audio.bpm,audio.phase_at,110.1)==.2
    asyncio.run(scenario())


def test_brightness_only_changes_all_dimmable_lamps_without_color_writes(monkeypatch):
    import app.animation.engine as module
    now=[100.];monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    async def scenario():
        client=MockClient();client.lights[0].xy=[.2,.3]
        engine=Engine(client);engine.settings=Settings(mode='music_brightness',bpm=120,bpm_source='manual')
        await engine.start()
        assert engine.stream is None and len(client.commands)==4
        assert all('color' not in p and 'color_temperature' not in p for _,p in client.commands)
        assert all(p['dynamics']['duration']==1000 for _,p in client.commands)
        now[0]=100.2;await engine.apply();assert len(client.commands)==4
        now[0]=102.01;await engine.apply()
        assert [l.brightness for l in client.lights]==[12,12,5,5]
        assert client.lights[0].xy==[.2,.3]
        now[0]=103;await engine.apply();assert len(client.commands)==8
        now[0]=104.02;await engine.apply()
        assert [l.brightness for l in client.lights]==[60,60,25,25]
        await engine.restore()
        assert all(not l.on for l in client.lights)
        assert engine.white_task is None
    asyncio.run(scenario())


def test_streaming_whites_have_music_or_steady_brightness_targets(monkeypatch):
    import app.animation.engine as module
    now=[100.];monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    async def scenario():
        client=MockClient();engine=Engine(client)
        engine.settings=Settings(mode='beat_flash',bpm=120,white_response='music',bpm_source='manual')
        await engine.start()
        assert len(client.commands)==2
        now[0]=102.1
        await engine.rest_music([l for l in engine.lights if not l.color])
        assert [l.brightness for l in client.lights[2:]]==[5,5]
        count=len(client.commands);now[0]=102.3
        await engine.rest_music([l for l in engine.lights if not l.color])
        assert len(client.commands)==count
        now[0]=104.2
        await engine.rest_music([l for l in engine.lights if not l.color],steady=True)
        assert [l.brightness for l in client.lights[2:]]==[25,25]
        assert all(id in ('2','3') and 'color' not in p for id,p in client.commands)
        await engine.pause();assert engine.white_task is None
    asyncio.run(scenario())


def test_white_rest_does_not_block_fast_color_frames():
    async def scenario():
        entered=asyncio.Event();release=asyncio.Event()
        class Client(MockClient):
            block=False
            async def update(self,id,payload):
                if self.block and id=='2':
                    entered.set();await release.wait()
                await super().update(id,payload)
        client=Client();engine=Engine(client);engine.settings=Settings(mode='beat_flash',bpm_source='manual')
        await engine.start();client.block=True
        task=asyncio.create_task(engine.rest_music([l for l in engine.lights if not l.color],force=True))
        try:
            await asyncio.wait_for(entered.wait(),.5)
            before=len(engine.stream.frames)
            await asyncio.wait_for(engine.apply(),.5)
            assert len(engine.stream.frames)==before+1
        finally:
            release.set();await task;await engine.stop()
        assert engine.white_task is None
    asyncio.run(scenario())


def test_white_only_brightness_mode_needs_no_entertainment_area():
    async def scenario():
        client=MockClient();engine=Engine(client);await engine.refresh()
        for light in engine.lights:light.enabled=not light.color
        engine.settings=Settings(mode='music_brightness')
        await engine.start()
        assert engine.stream is None
        assert {id for id,p in client.commands}=={'2','3'}
        await engine.stop()
    asyncio.run(scenario())


def test_brightness_mode_uses_audio_energy_before_bpm_and_pauses_on_audio_stop(monkeypatch,tmp_path):
    from app import main,config
    class Audio:
        running=False;phase_at=None
        def current_bpm(self):return None
        def music_energy(self):return .008 if self.running else None
        def status(self):return {'running':self.running,'bpm':None}
        async def start(self,id):self.running=True
        async def stop(self):self.running=False
    monkeypatch.setattr(config,'MOCK_MODE',True);monkeypatch.setattr(config,'MUSIC_ENABLED',False)
    monkeypatch.setattr(config,'DATA_DIR',tmp_path);monkeypatch.setattr(main,'audio',Audio())
    with TestClient(main.app) as client:
        assert client.put('/api/settings',json={'mode':'music_brightness','bpm_source':'audio'}).status_code==200
        assert client.post('/api/audio/start',json={'device_id':1}).status_code==200
        assert client.post('/api/control/start').status_code==200
        state=client.get('/api/state').json()
        assert all(5<l['brightness']<25 for l in state['lights'] if not l['color'])
        assert not state['streaming']
        assert client.post('/api/audio/stop').status_code==200
        assert client.get('/api/state').json()['status']=='paused'
