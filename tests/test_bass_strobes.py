import asyncio
from contextlib import suppress
import numpy as np
import pytest
from fastapi.testclient import TestClient
from app.audio.onsets import HitDetector
from app.animation.beats import HitFlashes, clock_flash_factors
from app.animation.engine import Engine
from app.hue.models import Settings
from app.hue.mock import MockClient
from app.hue.entertainment import EntertainmentSession


def bursts(sr, frequency, times, seconds=4):
    signal = np.zeros(round(sr*seconds))
    for at in times:
        start = round(at*sr); length = min(round(sr*.025), len(signal)-start)
        t = np.arange(length)/sr
        signal[start:start+length] += .2*np.sin(2*np.pi*frequency*t)*np.exp(-100*t)
    return signal


def analyze(signal, sr, block=1024):
    detector = HitDetector(sr); hits = []
    for start in range(0,len(signal),block):
        chunk = signal[start:start+block]
        hits.extend(detector.process(chunk,(start+len(chunk))/sr))
    return hits


@pytest.mark.parametrize('sr',[12000,44100,48000])
def test_bass_attacks_and_drum_rolls_are_distinguished(sr):
    kicks = [.3,.8,1.3,1.8,3.2]
    hits = analyze(bursts(sr,80,kicks),sr)
    bass = [hit for hit in hits if hit['kind']=='bass']
    assert len(bass)==len(kicks)
    assert np.allclose([hit['at'] for hit in bass], kicks, atol=.03)
    assert not [hit for hit in hits if hit['kind']=='drum']
    rolls = np.arange(.3,3.5,.125)
    hits = analyze(bursts(sr,2000,rolls),sr)
    assert len([hit for hit in hits if hit['kind']=='drum'])>=len(rolls)-1
    assert not [hit for hit in hits if hit['kind']=='bass']


def test_hit_detection_handles_chunk_boundaries_silence_and_sustained_bass():
    sr=48000; signal=bursts(sr,80,[.3,.8,1.3])
    small = analyze(signal,sr,257); large = analyze(signal,sr,4096)
    assert [hit['kind'] for hit in small] == [hit['kind'] for hit in large]
    assert [hit['at'] for hit in small] == pytest.approx([hit['at'] for hit in large])
    assert analyze(np.zeros(sr*2),sr)==[]
    assert analyze(np.random.default_rng(7).normal(0,.0001,sr*2),sr)==[]
    sustained = np.zeros(sr*3)
    sustained[sr//2:] = .2*np.sin(2*np.pi*80*np.arange(len(sustained)-sr//2)/sr)
    hits = analyze(sustained,sr)
    assert len(hits)==1 and hits[0]['kind']=='bass'


def hit(id,at,kind='bass',strength=10):
    return dict(id=id,at=at,kind=kind,strength=strength)


def test_hit_flashes_alternate_merge_double_hits_and_expire():
    effect=HitFlashes()
    first=[hit(1,1),hit(2,1,'drum')]
    assert effect.factors(first,1,2,'bass_drums','staggered')==[1,.2]
    assert effect.flashes==1
    assert effect.factors(first,1.15,2,'bass_drums','staggered')==[.2,.2]
    assert effect.factors([hit(3,1.5)],1.5,2,'bass_drums','staggered')==[.2,1]
    assert effect.factors([hit(4,2)],2,2,'bass','together')==[1,1]
    assert effect.factors([],2.2,2,'bass','together')==[.2,.2]


def test_hit_filters_sensitivity_subdivision_stale_events_and_rate_cap():
    effect=HitFlashes()
    assert effect.factors([hit(1,1,'drum')],1,2,'bass','together')==[.2,.2]
    assert effect.factors([hit(2,1.5,strength=2)],1.5,2,'bass','together',sensitivity=10)==[.2,.2]
    assert effect.factors([hit(3,2,strength=2)],2,2,'bass','together',sensitivity=100)==[1,1]
    assert effect.factors([hit(4,2.5)],3,2,'bass','together')==[.2,.2]
    effect.reset(); accepted=[]
    for i in range(40):
        at=1+i*.1; before=effect.flashes
        effect.factors([hit(i+1,at)],at,2,'bass','staggered')
        if effect.flashes>before: accepted.append(at)
    assert all(delta>=1/3 for delta in np.diff(accepted))
    effect.reset()
    assert effect.factors([hit(1,1)],1,2,'bass','together',division=2)==[1,1]
    assert effect.factors([hit(2,1.5)],1.5,2,'bass','together',division=2)==[.2,.2]
    assert effect.factors([hit(3,2)],2,2,'bass','together',division=2)==[1,1]


def test_bpm_clock_can_alternate_color_bulbs():
    assert clock_flash_factors(120,0,0,2,'staggered')==[1,.2]
    assert clock_flash_factors(120,.5,0,2,'staggered')==[.2,1]
    assert clock_flash_factors(120,1,0,2,'staggered')==[1,.2]
    assert clock_flash_factors(120,.5,0,2,'together')==[1,1]


def test_start_wakes_idle_animation_loop_for_prompt_stream_frames():
    async def scenario():
        next_frame = asyncio.Event()
        class Session(EntertainmentSession):
            async def send(self, values):
                await super().send(values)
                if len(self.frames)>1: next_frame.set()
        engine=Engine(MockClient());engine.stream_factory=Session
        task=asyncio.create_task(engine.run())
        await asyncio.sleep(0)
        try:
            engine.settings=Settings(mode='beat_flash')
            await engine.start()
            await asyncio.wait_for(next_frame.wait(),timeout=.5)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError): await task
            await engine.stop()
    asyncio.run(scenario())


def test_bass_engine_starts_without_bpm_and_only_flashes_color_bulbs(monkeypatch):
    import app.animation.engine as engine_module
    now=[200.]
    monkeypatch.setattr(engine_module.time,'monotonic',lambda:now[0])
    class Audio:
        running=False; hit_counter=0; events=[]
        def current_bpm(self): return None
        def recent_hits(self,after): return [event for event in self.events if event['id']>after]
    async def scenario():
        client=MockClient();engine=Engine(client);engine.audio=Audio()
        engine.settings=Settings(mode='beat_flash',flash_trigger='bass',flash_pattern='staggered',bpm_source='audio')
        with pytest.raises(ValueError,match='Start listening'): await engine.start()
        engine.audio.running=True
        await engine.start()
        rest_count=len(client.commands)
        assert [light.brightness for light in client.lights[:2]]==[12,12]
        engine.audio.events=[hit(1,200.)]
        await engine.apply()
        assert [light.brightness for light in client.lights[:2]]==[60,12]
        now[0]=200.5;engine.audio.events.append(hit(2,200.5))
        await engine.apply()
        assert [light.brightness for light in client.lights[:2]]==[12,60]
        assert len(client.commands)==rest_count
        assert [light.brightness for light in client.lights[2:]]==[25,25]
        await engine.restore()
        assert engine.stream is None and all(not light.on for light in client.lights)
    asyncio.run(scenario())


def test_stopping_audio_pauses_bass_flashes_even_with_manual_bpm(monkeypatch,tmp_path):
    from app import config,main
    class Audio:
        running=False; hit_counter=0
        def current_bpm(self): return None
        def recent_hits(self,after): return []
        def status(self): return {'running':self.running,'bpm':None}
        async def start(self,id): self.running=True
        async def stop(self): self.running=False
    monkeypatch.setattr(config,'MOCK_MODE',True);monkeypatch.setattr(config,'MUSIC_ENABLED',False)
    monkeypatch.setattr(config,'DATA_DIR',tmp_path);monkeypatch.setattr(main,'audio',Audio())
    with TestClient(main.app) as client:
        assert client.put('/api/settings',json={'mode':'beat_flash','flash_trigger':'bass_drums','flash_pattern':'staggered','bpm_source':'manual'}).status_code==200
        assert client.post('/api/control/start').status_code==400
        assert client.post('/api/audio/start',json={'device_id':1}).status_code==200
        assert client.post('/api/control/start').status_code==200
        assert client.post('/api/audio/stop').status_code==200
        state=client.get('/api/state').json()
        assert state['status']=='paused' and not state['streaming']
