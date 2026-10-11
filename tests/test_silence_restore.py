import asyncio
from contextlib import suppress
import pytest
from app.animation.engine import Engine
from app.hue.mock import MockClient
from app.hue.models import Settings


class Audio:
    running=True
    energy=.02
    bpm=120
    phase_at=100
    hit_counter=0
    def music_energy(self):return self.energy
    def current_bpm(self):return self.bpm
    def recent_hits(self,after=0):return []


def original_states(client):
    return [(l.id,l.on,l.brightness,l.xy) for l in client.lights]


@pytest.mark.parametrize('mode',['static','rotate','beat_gentle','beat_flash','music_brightness'])
def test_silence_restores_then_resumes_without_losing_original_state(monkeypatch,mode):
    import app.animation.engine as module
    now=[100.];monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    async def scenario():
        client=MockClient()
        for i,l in enumerate(client.lights):
            l.on=i%2==0;l.brightness=30+i
            if l.color:l.xy=[.2,.3]
        original=original_states(client)
        engine=Engine(client);engine.audio=Audio()
        engine.settings=Settings(mode=mode,bpm_source='audio')
        await engine.start();previous_stream=engine.stream
        assert engine.status=='running'
        engine.audio.energy=0
        now[0]=101.;await engine.check_audio()
        now[0]=102.5;await engine.check_audio()
        assert engine.status=='running'  # A short gap leaves playback intact.
        now[0]=103.1;await engine.check_audio()
        assert engine.status=='waiting_audio' and engine.stream is None and engine.white_task is None
        assert original_states(client)==original and engine.audio.running
        assert len(engine.snapshot)==4
        count=len(client.commands);now[0]=104.;await engine.check_audio()
        assert len(client.commands)==count  # Do not repeatedly restore during silence.
        engine.audio.energy=.02
        now[0]=105.;await engine.check_audio()
        now[0]=105.6;await engine.check_audio()
        assert engine.status=='running'
        if previous_stream:assert engine.stream is not previous_stream
        assert len(engine.snapshot)==4
        engine.audio.energy=0
        now[0]=106.;await engine.check_audio()
        now[0]=108.1;await engine.check_audio()
        assert original_states(client)==original
        await engine.restore()
        assert engine.status=='stopped' and engine.snapshot is None
        engine.audio.energy=.02
        now[0]=109.;await engine.check_audio()
        now[0]=110.;await engine.check_audio()
        assert engine.status=='stopped'  # Explicit Restore cancels automatic resume.
    asyncio.run(scenario())


def test_silent_start_keeps_lights_unchanged_and_waits_for_bpm(monkeypatch):
    import app.animation.engine as module
    now=[100.];monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    async def scenario():
        client=MockClient();original=original_states(client)
        engine=Engine(client);engine.audio=Audio();engine.audio.energy=0;engine.audio.bpm=None
        engine.settings=Settings(mode='beat_gentle',bpm_source='audio')
        await engine.start()
        assert engine.status=='waiting_audio' and not client.commands
        assert original_states(client)==original and engine.stream is None
        engine.audio.energy=.02
        now[0]=101.;await engine.check_audio()
        now[0]=101.6;await engine.check_audio()
        assert engine.status=='waiting_audio' and 'reliable BPM' in engine.playback_message
        engine.audio.bpm=120
        now[0]=102.;await engine.check_audio()
        assert engine.status=='running' and engine.stream is not None
        await engine.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('energy',[None,0])
def test_missing_or_silent_input_restores_but_manual_playback_without_capture_does_not(monkeypatch,energy):
    import app.animation.engine as module
    now=[100.];monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    async def scenario():
        client=MockClient();engine=Engine(client);engine.audio=Audio()
        await engine.start();engine.audio.energy=energy
        now[0]=101.;await engine.check_audio()
        now[0]=103.1;await engine.check_audio()
        assert engine.status=='waiting_audio' and all(not l.on for l in client.lights)
        await engine.stop();engine.audio.running=False
        await engine.start()
        now[0]=104.;await engine.check_audio()
        now[0]=110.;await engine.check_audio()
        assert engine.status=='running'
        await engine.stop()
    asyncio.run(scenario())


def test_restore_failure_keeps_snapshot_and_retries_before_resuming(monkeypatch):
    import app.animation.engine as module
    now=[100.];monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    async def scenario():
        class Client(MockClient):
            fail=False
            async def update(self,id,payload):
                if self.fail and id=='2':raise ValueError('bridge temporarily unavailable')
                await super().update(id,payload)
        client=Client();engine=Engine(client);engine.audio=Audio()
        await engine.start();engine.audio.energy=0
        now[0]=101.;await engine.check_audio();client.fail=True
        now[0]=103.1
        with pytest.raises(ValueError):await engine.check_audio()
        assert engine.snapshot and not engine.waiting_restored
        client.fail=False;now[0]=104.;await engine.check_audio()
        assert engine.waiting_restored and all(not l.on for l in client.lights)
        await engine.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode',['static','rotate','crossfade','breathe','bpm'])
def test_white_background_worker_changes_brightness_in_regular_color_modes(mode):
    async def scenario():
        white_updated=asyncio.Event()
        class Client(MockClient):
            async def update(self,id,payload):
                await super().update(id,payload)
                if id=='3':white_updated.set()
        client=Client();engine=Engine(client);engine.audio=Audio()
        engine.settings=Settings(mode=mode,bpm_source='audio',white_fill=60)
        factor=[1.];engine.music_factor=lambda now:factor[0]
        await engine.start()
        assert [l.brightness for l in client.lights[2:]]==[60,60]
        factor[0]=.2;white_updated.clear()
        engine.rest_brightness={id:(value,at-2.1) for id,(value,at) in engine.rest_brightness.items()}
        await asyncio.wait_for(white_updated.wait(),1)
        assert [l.brightness for l in client.lights[2:]]==[12,12]
        before=len([p for id,p in client.commands if id in ('2','3')])
        await engine.apply(5)
        assert len([p for id,p in client.commands if id in ('2','3')])==before
        # The color worker must not overwrite the music target for white bulbs.
        engine.settings.white_fill=80;factor[0]=1.;white_updated.clear()
        engine.rest_brightness={id:(value,at-2.1) for id,(value,at) in engine.rest_brightness.items()}
        await asyncio.wait_for(white_updated.wait(),1)
        assert [l.brightness for l in client.lights[2:]]==[80,80]
        assert all('color' not in p and 'color_temperature' not in p for id,p in client.commands if id in ('2','3'))
        await engine.stop();assert engine.white_task is None
    asyncio.run(scenario())


def test_static_run_loop_supervises_silence_and_manual_pause_cancels_resume():
    async def scenario():
        engine=Engine(MockClient());engine.audio=Audio()
        await engine.start()
        engine.audio.energy=0
        import time
        engine.silence_since=time.monotonic()-3
        task=asyncio.create_task(engine.run())
        try:
            for _ in range(100):
                if engine.status=='waiting_audio':break
                await asyncio.sleep(.01)
            assert engine.status=='waiting_audio'
            await engine.pause();engine.audio.energy=.02
            await asyncio.sleep(.7)
            assert engine.status=='paused' and engine.stream is None
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):await task
            await engine.stop()
    asyncio.run(scenario())
