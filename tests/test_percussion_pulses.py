import asyncio
from types import SimpleNamespace
import numpy as np
import pytest
from app.audio.percussion import PercussionDetector
from app.audio.live import sample_end_time
from app.animation.beats import clock_gentle_factors,HitFlashes,palette_color
from app.animation.engine import Engine
from app.hue.models import Settings
from app.hue.mock import MockClient


def percussion_signal(sr,times,seconds=5,bed=0):
    rng=np.random.default_rng(42);samples=rng.normal(0,bed,round(sr*seconds))
    for at in times:
        n=min(round(sr*.04),len(samples)-round(at*sr));t=np.arange(n)/sr
        samples[round(at*sr):round(at*sr)+n]+=.2*rng.normal(0,1,n)*np.exp(-80*t)
    return samples


def analyze(samples,sr,block=513):
    detector=PercussionDetector(sr);hits=[];drops=[]
    for start in range(0,len(samples),block):
        chunk=samples[start:start+block]
        hits.extend(detector.process(chunk,(start+len(chunk))/sr));drops.extend(detector.drops)
    return hits,drops


@pytest.mark.parametrize('sr',[12000,44100,48000])
def test_percussion_attacks_and_rolls_follow_transients_within_30ms(sr):
    times=np.arange(.3,4,.125);hits,drops=analyze(percussion_signal(sr,times),sr)
    assert len(hits)>=len(times)-1
    for at in times:assert min(abs(hit['at']-at) for hit in hits)<.03
    assert all(h['kind']=='percussion' for h in hits)
    assert not drops


def test_percussion_rejects_sustained_bass_and_low_noise_and_handles_chunks():
    sr=12000;t=np.arange(sr*5)/sr
    assert analyze(.1*np.sin(2*np.pi*80*t),sr)[0]==[]
    assert analyze(percussion_signal(sr,[],bed=.0001),sr)[0]==[]
    signal=percussion_signal(sr,[.3,.8,1.3,2.1])
    small=analyze(signal,sr,127)[0];large=analyze(signal,sr,4096)[0]
    assert [h['at'] for h in small]==pytest.approx([h['at'] for h in large])


def test_drop_requires_a_sustained_percussive_energy_lift_and_has_a_cooldown():
    sr=12000;rng=np.random.default_rng(7)
    signal=percussion_signal(sr,[3,3.25,3.5,3.75,4,4.25],seconds=8,bed=.001)
    signal[3*sr:6*sr]+=rng.normal(0,.035,3*sr)
    _,drops=analyze(signal,sr)
    assert len(drops)==1 and 2.98<=drops[0]['at']<=3.1
    isolated=percussion_signal(sr,[3],seconds=8,bed=.001)
    assert analyze(isolated,sr)[1]==[]
    assert analyze(percussion_signal(sr,[],seconds=8,bed=.02),sr)[1]==[]


def test_gentle_stagger_alternates_peaks_and_interpolates_between_them():
    assert clock_gentle_factors(120,0,0,2,'staggered')==pytest.approx([1,.2])
    assert clock_gentle_factors(120,.5,0,2,'staggered')==pytest.approx([.2,1])
    assert clock_gentle_factors(120,.25,0,2,'staggered')==pytest.approx([.6,.6])
    assert clock_gentle_factors(120,1,0,2,'staggered')==pytest.approx([1,.2])
    assert clock_gentle_factors(120,.5,0,2,'together')==[1,1]
    assert clock_gentle_factors(120,.25,0,1,'staggered')==pytest.approx([.2])


def test_percussion_gentle_pulses_use_attack_timestamps_and_delay_without_consuming_future_hits():
    effect=HitFlashes();hit={'id':1,'at':1.,'kind':'percussion','strength':10}
    assert effect.factors([hit],1,2,'percussion','staggered',mode='beat_gentle',offset=.05)==[.2,.2]
    assert effect.cursor==0
    assert effect.factors([hit],1.05,2,'percussion','staggered',mode='beat_gentle',offset=.05)==[1,.2]
    values=effect.factors([],1.2,2,'percussion','staggered',mode='beat_gentle',offset=.05)
    assert .2<values[0]<1 and values[1]==.2
    second={'id':2,'at':1.25,'kind':'percussion','strength':10}
    assert effect.factors([second],1.3,2,'percussion','staggered',mode='beat_gentle',offset=.05)[1]==1
    assert effect.factors([],2,2,'percussion','staggered',mode='beat_gentle')==[.2,.2]
    effect.reset()
    late=effect.factors([hit],1.075,2,'percussion','together',mode='beat_gentle')
    assert .2<late[0]<1  # A late frame advances the envelope instead of restarting it.
    assert effect.factors([{'id':3,'at':2,'kind':'bass','strength':100}],2,2,'percussion','together')==[.2,.2]


def test_portaudio_sample_timestamps_account_for_capture_buffer_age():
    timing=SimpleNamespace(inputBufferAdcTime=10,currentTime=10.05)
    assert sample_end_time(timing,480,48000,100)==pytest.approx(99.96)
    assert sample_end_time(None,480,48000,100)==100
    assert sample_end_time(SimpleNamespace(inputBufferAdcTime=0,currentTime=0),480,48000,100)==100


def test_gentle_engine_cycles_colors_staggers_whites_and_applies_drop_once(monkeypatch):
    import app.animation.engine as module
    now=[100.];monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    class Audio:
        running=True;phase_at=100;hit_counter=0;drop_counter=0;drops=[]
        def current_bpm(self):return 120
        def music_energy(self):return .02
        def recent_hits(self,after=0):return []
        def recent_drops(self,after=0):return [d for d in self.drops if d['id']>after]
    async def scenario():
        client=MockClient();engine=Engine(client);engine.audio=Audio()
        engine.palette=['#ff0000','#0000ff','#00ff00','#ffff00']
        engine.settings=Settings(mode='beat_gentle',bpm_source='audio',flash_pattern='staggered',white_pattern='alternate',palette_every=1)
        await engine.start()
        assert [l.brightness for l in client.lights[:2]]==[60,12]
        assert [l.brightness for l in client.lights[2:]]==[25,5]
        color=engine.lights[0].xy.copy()
        now[0]=100.5;await engine.apply()
        assert [l.brightness for l in client.lights[:2]]==pytest.approx([12,60])
        assert engine.lights[0].xy!=color
        now[0]=102.1;await engine.rest_music([l for l in engine.lights if not l.color])
        assert [l.brightness for l in client.lights[2:]]==[5,25]
        engine.audio.drops=[{'id':1,'at':102.1}]
        await engine.apply();assert engine.palette_shift==1
        await engine.apply();assert engine.palette_shift==1
        await engine.stop()
    asyncio.run(scenario())


def test_gentle_percussion_can_start_before_bpm_and_respects_fixed_colors(monkeypatch):
    import app.animation.engine as module
    now=[100.];monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    class Audio:
        running=True;phase_at=None;hit_counter=0
        def current_bpm(self):return None
        def music_energy(self):return .02
        def recent_hits(self,after=0):return [{'id':1,'at':100.,'kind':'percussion','strength':10}] if after<1 else []
    async def scenario():
        engine=Engine(MockClient());engine.audio=Audio()
        engine.settings=Settings(mode='beat_gentle',bpm_source='audio',flash_trigger='percussion',pulse_palette='fixed')
        await engine.start();assert engine.stream is not None
        # The first new percussion attack drives the pulse without a tempo estimate.
        assert [l.brightness for l in engine.client.lights[:2]]==[60,60]
        xy=engine.lights[0].xy.copy();now[0]=100.2;await engine.apply()
        assert 12<engine.lights[0].brightness<60 and engine.lights[0].xy==xy
        await engine.stop()
    asyncio.run(scenario())


def test_palette_interpolation_uses_all_colors_and_wraps():
    assert palette_color(['#ff0000','#0000ff'],.5)=='#800080'
    assert palette_color(['#ff0000','#0000ff'],2)=='#ff0000'


def test_drop_accent_extends_clock_flash_without_adding_an_offbeat_flash():
    from app.animation.beats import clock_flash_factors
    assert clock_flash_factors(120,.175,0,2,'staggered')==[.2,.2]
    assert clock_flash_factors(120,.175,0,2,'staggered',accent=True)==[1,.2]
    assert clock_flash_factors(120,.3,0,2,'staggered',accent=True)==[.2,.2]
    assert clock_flash_factors(120,.5,0,2,'staggered',accent=True)==[.2,1]
