import asyncio
import pytest
from fastapi.testclient import TestClient

from app.animation.engine import Engine
from app.hue.mock import MockClient
from app.hue.models import Light, Settings


class Audio:
    running=True
    energy=.02
    phase_at=100
    hit_counter=0
    def music_energy(self): return self.energy
    def current_bpm(self): return 120
    def recent_hits(self,after=0): return []
    def status(self): return {'running':self.running}
    async def stop(self): self.running=False


def states(client):
    return [(light.on,light.brightness,light.xy) for light in client.lights]


@pytest.mark.parametrize('mode',['static','rotate','beat_gentle','beat_flash','music_brightness'])
def test_unselected_bulbs_are_off_during_music_and_restored_during_silence(monkeypatch,mode):
    import app.animation.engine as module
    now=[100.];monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    async def scenario():
        client=MockClient()
        for i,light in enumerate(client.lights):
            light.on=True;light.brightness=30+i;light.enabled=i%2==0
            if light.color: light.xy=[.2,.3]
        original=states(client)
        engine=Engine(client);engine.audio=Audio()
        engine.settings=Settings(mode=mode)
        await engine.start()
        assert [light.on for light in client.lights]==[True,False,True,False]
        off=[payload for id,payload in client.commands if id in ('1','3')]
        assert len(off)==2
        assert all(payload=={'on':{'on':False},'dynamics':{'duration':1000}} for payload in off)
        engine.audio.energy=0
        now[0]=101.;await engine.check_audio()
        now[0]=103.1;await engine.check_audio()
        assert engine.status=='waiting_audio' and states(client)==original
        before=len(client.commands)
        now[0]=104.;await engine.check_audio();await engine.off_unselected()
        assert len(client.commands)==before
        engine.audio.energy=.02
        now[0]=105.;await engine.check_audio()
        now[0]=105.6;await engine.check_audio()
        assert engine.status=='running'
        assert [light.on for light in client.lights]==[True,False,True,False]
        await engine.restore()
        assert states(client)==original and engine.snapshot is None
        await engine.off_unselected()
        assert states(client)==original
    asyncio.run(scenario())


def test_silent_start_and_waiting_selection_changes_leave_unselected_bulbs_alone(monkeypatch):
    import app.animation.engine as module
    now=[100.];monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    async def scenario():
        client=MockClient()
        for light in client.lights: light.on=True
        client.lights[1].enabled=False
        engine=Engine(client);engine.audio=Audio();engine.audio.energy=0
        await engine.start()
        engine.lights[3].enabled=False
        await engine.reconfigure_lights()
        assert engine.status=='waiting_audio' and not client.commands
        assert all(light.on for light in client.lights)
        engine.audio.energy=.02
        now[0]=101.;await engine.check_audio()
        now[0]=101.6;await engine.check_audio()
        assert [light.on for light in client.lights]==[True,False,True,False]
        await engine.restore()
        assert all(light.on for light in client.lights)
    asyncio.run(scenario())


@pytest.mark.parametrize('waiting_at_start',[True,False])
def test_floor_lamp_state_is_captured_when_music_starts_after_waiting(monkeypatch,waiting_at_start):
    import app.animation.engine as module
    now=[100.];monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    async def scenario():
        client=MockClient()
        lamp=Light(id='floor',name='Floor lamp',dimming=True,on=False,brightness=20)
        client.lights.append(lamp)
        for light in client.lights:
            if light.color:light.xy=[.2,.3]
        engine=Engine(client);engine.audio=Audio()
        engine.settings=Settings(mode='beat_gentle')
        engine.audio.energy=0 if waiting_at_start else .02
        await engine.start()
        if not waiting_at_start:
            engine.audio.energy=0
            now[0]=101.;await engine.check_audio()
            now[0]=103.1;await engine.check_audio()
        assert engine.status=='waiting_audio'
        # The user turns a lamp on while music is stopped, before the next song.
        lamp.on=True;lamp.brightness=43
        original=states(client)
        engine.audio.energy=.02
        now[0]=105.;await engine.check_audio()
        now[0]=105.6;await engine.check_audio()
        assert engine.status=='running' and not lamp.on
        engine.audio.energy=0
        now[0]=106.;await engine.check_audio()
        now[0]=108.1;await engine.check_audio()
        assert engine.status=='waiting_audio'
        assert states(client)==original
        assert lamp.on and lamp.brightness==43
        await engine.restore()
    asyncio.run(scenario())


def test_repeated_start_while_silent_keeps_lamp_changes_and_restore_uses_playback_state(monkeypatch):
    import app.animation.engine as module
    now=[100.];monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    async def scenario():
        client=MockClient();client.lights[3].enabled=False
        for light in client.lights:
            if light.color:light.xy=[.2,.3]
        engine=Engine(client);engine.audio=Audio();engine.audio.energy=0
        await engine.start()
        client.lights[3].on=True;client.lights[3].brightness=42
        await engine.start()
        assert not client.commands and client.lights[3].on
        original=states(client)
        engine.audio.energy=.02
        now[0]=101.;await engine.check_audio()
        now[0]=101.6;await engine.check_audio()
        assert not client.lights[3].on
        await engine.restore()
        assert states(client)==original
    asyncio.run(scenario())


@pytest.mark.parametrize('mode',['static','beat_flash','music_brightness'])
def test_selection_changes_apply_immediately_and_allow_deselecting_every_bulb(monkeypatch,tmp_path,mode):
    from app import config,main
    backend=MockClient()
    for light in backend.lights:
        light.on=True
        if light.color: light.xy=[.2,.3]
    original=states(backend)
    monkeypatch.setattr(config,'MOCK_MODE',True)
    monkeypatch.setattr(config,'MUSIC_ENABLED',False)
    monkeypatch.setattr(config,'DATA_DIR',tmp_path)
    monkeypatch.setattr(main,'MockClient',lambda:backend)
    monkeypatch.setattr(main,'audio',Audio())
    with TestClient(main.app) as client:
        assert client.put('/api/settings',json={'mode':mode}).status_code==200
        assert client.post('/api/control/start').status_code==200
        snapshot=main.engine.snapshot
        for light in backend.lights:
            response=client.put('/api/lights/'+light.id,json={'name':light.name,'role':light.role,'enabled':False})
            assert response.status_code==200
            assert not light.on
        assert main.engine.status=='running' and main.engine.stream is None
        assert main.engine.snapshot is snapshot
        light=backend.lights[0]
        assert client.put('/api/lights/0',json={'name':light.name,'role':light.role,'enabled':True}).status_code==200
        assert light.on and all(not light.on for light in backend.lights[1:])
        if mode=='beat_flash': assert set(main.engine.stream.channels.values())=={'0'}
        assert client.post('/api/control/restore').status_code==200
        assert states(backend)==original


@pytest.mark.parametrize('mode',['static','beat_flash'])
def test_background_worker_keeps_unselected_bulbs_off_without_blocking_color_frames(mode):
    async def scenario():
        entered=asyncio.Event();release=asyncio.Event();completed=asyncio.Event()
        class Client(MockClient):
            block=False
            async def update(self,id,payload):
                if self.block and id=='3':
                    entered.set();await release.wait()
                await super().update(id,payload)
                if self.block and id=='3': completed.set()
        client=Client();client.lights[3].enabled=False
        engine=Engine(client);engine.settings=Settings(mode=mode,bpm_source='manual',white_response='steady')
        await engine.start()
        # Simulate another controller switching an unselected lamp on. The engine's
        # cached state is still off, so maintaining off must not depend on that cache.
        client.lights[3].on=True;client.block=True;engine.rest_off['3']-=2.1
        try:
            await asyncio.wait_for(entered.wait(),1)
            if mode=='beat_flash':
                before=len(engine.stream.frames)
                await asyncio.wait_for(engine.apply(),.5)
                assert len(engine.stream.frames)==before+1
            release.set();await asyncio.wait_for(completed.wait(),1)
            assert not client.lights[3].on
        finally:
            release.set();await engine.stop()
    asyncio.run(scenario())


def test_off_updates_are_slow_support_on_only_bulbs_and_retry_failures(monkeypatch):
    import app.animation.engine as module
    now=[100.];monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    async def scenario():
        class Client(MockClient):
            fail=False
            async def update(self,id,payload):
                if self.fail and id=='extra': raise ValueError('bridge unavailable')
                await super().update(id,payload)
        client=Client();client.lights.append(Light(id='extra',name='On only',on=True))
        engine=Engine(client);await engine.start()
        def off_count(): return len([id for id,_ in client.commands if id=='extra'])
        assert off_count()==1 and not client.lights[-1].on
        client.lights[-1].on=True
        now[0]=101.9;await engine.off_unselected()
        assert off_count()==1
        now[0]=102.;client.fail=True
        with pytest.raises(ValueError,match='bridge unavailable'): await engine.off_unselected()
        client.fail=False;now[0]=102.1;await engine.off_unselected()
        assert off_count()==2 and not client.lights[-1].on
        payload=next(payload for id,payload in client.commands if id=='extra')
        assert set(payload)=={'on','dynamics'}
        await engine.restore()
        assert client.lights[-1].on
    asyncio.run(scenario())


def test_pause_and_stop_cancel_off_maintenance():
    async def scenario():
        client=MockClient();client.lights[3].enabled=False
        engine=Engine(client);await engine.start()
        for action in (engine.pause,engine.stop):
            await action()
            client.lights[3].on=True
            before=len(client.commands)
            await engine.off_unselected()
            assert client.lights[3].on and len(client.commands)==before
            assert engine.white_task is None
        await engine.restore()
    asyncio.run(scenario())
