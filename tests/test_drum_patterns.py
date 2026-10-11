import asyncio
import numpy as np
import pytest
from scipy.signal import butter, sosfilt
from fastapi.testclient import TestClient
from app.audio.percussion import PercussionDetector, RollTracker
from app.animation.beats import HitFlashes
from app.animation.engine import Engine
from app.hue.models import Settings
from app.hue.mock import MockClient
from tests.test_percussion_pulses import percussion_signal


def snare_mix(sr=48000, times=None, seconds=4):
    if times is None: times = np.arange(.4, 3.6, .125)
    t = np.arange(round(sr * seconds)) / sr
    rng = np.random.default_rng(8)
    signal = .22*np.sin(2*np.pi*80*t) + .05*np.sin(2*np.pi*440*t) + rng.normal(0,.003,len(t))
    filter = butter(2, [500,min(8000,sr*.45)], btype='bandpass', fs=sr, output='sos')
    for at in times:
        n = min(round(.06*sr),len(signal)-round(at*sr)); u=np.arange(n)/sr
        noise = sosfilt(filter,rng.normal(0,1,n))
        signal[round(at*sr):round(at*sr)+n] += .03*noise*np.exp(-60*u)
    return signal


def analyze(signal, sr=48000, block=257, **kwargs):
    detector=PercussionDetector(sr);hits=[];drops=[]
    for i in range(0,len(signal),block):
        chunk=signal[i:i+block]
        hits.extend(detector.process(chunk,(i+len(chunk))/sr,**kwargs));drops.extend(detector.drops)
    return detector,hits,drops


@pytest.mark.parametrize('sr',[12000,44100,48000])
def test_quiet_snare_roll_survives_loud_bass_and_pad(sr):
    times=np.arange(.4,3.6,.125)
    detector,hits,_=analyze(snare_mix(sr,times),sr)
    assert len(hits)==len(times)
    assert all(min(abs(hit['at']-at) for hit in hits)<.03 for at in times)
    assert all(hit['qualified'] for hit in hits)
    # Instrument labels are approximate; continuity follows spectral shape.
    assert sum(hit['family']=='snare' for hit in hits)>=len(times)*.9
    assert sum(hit['roll'] for hit in hits)>=len(times)-4
    assert not detector.status(4)['roll_active']


def test_roll_tracking_accepts_acceleration_and_rejects_gaps_or_different_timbres():
    tracker=RollTracker();at=0
    for interval in [.2,.18,.15,.12,.1]:
        at+=interval;rolling=tracker.add(at,[0,1,2,1],'snare')
    assert rolling and tracker.status(at)['roll_rate']>6
    assert not tracker.add(at+.1,[4,0,0,0],'kick')
    assert not tracker.status(at+.1)['roll_active']
    assert not tracker.add(at+1,[0,1,2,1],'snare')


def test_short_drums_and_chunk_boundaries_preserve_roll_labels_and_times():
    times=np.arange(.4,2,.0625);signal=snare_mix(times=times,seconds=3)
    _,small,_=analyze(signal,block=127)
    _,large,_=analyze(signal,block=4096)
    assert len(small)==len(times)
    assert [hit['at'] for hit in small]==pytest.approx([hit['at'] for hit in large])
    assert [hit['roll'] for hit in small]==[hit['roll'] for hit in large]
    assert sum(hit['roll'] for hit in small)>=len(times)-4


def test_vibrato_and_stationary_noise_do_not_become_drum_patterns():
    sr=12000;t=np.arange(sr*4)/sr
    vibrato=.2*np.sin(2*np.pi*np.cumsum(440+10*np.sin(2*np.pi*5*t))/sr)
    assert analyze(vibrato,sr)[1]==[]
    assert analyze(np.random.default_rng(7).normal(0,.02,len(t)),sr)[1]==[]
    assert analyze(np.zeros(len(t)),sr)[1]==[]


def test_sensitivity_changes_detection_of_small_attacks_against_background():
    sr=12000;rng=np.random.default_rng(7);times=np.arange(.3,3,.125)
    signal=rng.normal(0,.004,sr*4)
    for at in times:
        n=round(sr*.04)
        signal[round(at*sr):round(at*sr)+n]+=.006*rng.normal(0,1,n)*np.exp(-80*np.arange(n)/sr)
    low=analyze(signal,sr,sensitivity=1)[1];high=analyze(signal,sr,sensitivity=100)[1]
    assert len(high)>len(low)
    assert all(min(abs(hit['at']-at) for at in times)<.03 for hit in high)


def test_drop_uses_recent_phrase_contrast_and_repeated_attacks():
    sr=12000
    signal=percussion_signal(sr,np.arange(.3,6,.25),seconds=7,bed=.012)
    signal[3*sr:6*sr]*=2
    detector,_,drops=analyze(signal,sr,bpm=120)
    assert len(drops)==1 and 3<=drops[0]['at']<3.6
    assert .15<=drops[0]['confirmed_at']-drops[0]['at']<=.6
    assert detector.bar_seconds==2
    assert detector.status(drops[0]['confirmed_at']+.1)['drop_active']
    assert not detector.status(drops[0]['confirmed_at']+3)['drop_active']
    # A volume lift without repeated drum attacks is insufficient.
    rng=np.random.default_rng(3);noise=rng.normal(0,.012,sr*7);noise[3*sr:]*=2
    assert analyze(noise,sr,bpm=120)[2]==[]
    assert analyze(signal,sr,bpm=90)[0].bar_seconds==pytest.approx(240/90)


def roll_events(rate=8, seconds=2):
    return [dict(id=i+1,at=1+i/rate,kind='percussion',strength=1,qualified=True,
                 roll=True,roll_interval=1/rate) for i in range(round(rate*seconds))]


@pytest.mark.parametrize('pattern',['together','staggered'])
def test_eight_hz_roll_has_high_and_low_frames_at_actual_stream_rate(pattern):
    effect=HitFlashes();events=roll_events();frames=[];starts=[]
    for now in np.arange(1,3.3,.05):
        old=effect.flashes
        values=effect.factors(events,now,2,'percussion',pattern,roll_max_hz=8)
        frames.append(values)
        if effect.flashes>old: starts.append(effect.last_flash)
    assert len(starts)==16
    assert all(delta>=.125-1e-9 for delta in np.diff(starts))
    peaks=[i for i,values in enumerate(frames) if max(values)==1]
    assert len(peaks)==16
    assert all(b-a>=2 for a,b in zip(peaks,peaks[1:]))
    if pattern=='staggered':
        assert [frames[i].index(1) for i in peaks]==[0,1]*8
    assert frames[-1]==[.2,.2]


def test_roll_limit_subdivision_stale_hits_and_ordinary_attack_cap():
    effect=HitFlashes();events=roll_events(rate=16);accepted=[]
    for now in np.arange(1,3.3,.05):
        before=effect.flashes
        effect.factors(events,now,2,'percussion','together',roll_max_hz=8)
        if effect.flashes>before:accepted.append(effect.last_flash)
    assert len(accepted)==16
    assert min(np.diff(accepted))>=.125-1e-9
    effect.reset();events=roll_events()
    for now in np.arange(1,3.3,.05):effect.factors(events,now,2,'percussion','together',division=2)
    assert effect.flashes==8
    for rolling in [False,True]:
        effect.reset();accepted=[]
        for event in roll_events():
            event['roll']=rolling;before=effect.flashes
            effect.factors([event],event['at'],2,'percussion','together',roll_max_hz=3)
            if effect.flashes>before:accepted.append(effect.last_flash)
        assert all(delta>=1/3-1e-9 for delta in np.diff(accepted))
    effect.reset()
    assert effect.factors(roll_events(),10,2,'percussion','together')==[.2,.2]
    assert effect.flashes==0


def test_gentle_roll_decay_reveals_next_hit_and_drop_does_not_fill_sharp_gaps():
    event=roll_events()[0];effect=HitFlashes()
    assert effect.factors([event],1,2,'percussion','together',mode='beat_gentle')==[1,1]
    assert effect.factors([],1.11,2,'percussion','together',mode='beat_gentle')==[.2,.2]
    effect.reset()
    assert effect.factors([event],1,2,'percussion','together',accent=True)==[1,1]
    assert effect.factors([],1.075,2,'percussion','together',accent=True)==[.2,.2]


def test_engine_passes_roll_rate_and_keeps_white_updates_slow(monkeypatch):
    from app.animation import engine as module
    now=[100.];monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    class Audio:
        running=True;phase_at=None;hit_counter=0;events=[]
        def current_bpm(self):return None
        def music_energy(self):return .1
        def recent_hits(self,after):return [e for e in self.events if e['id']>after]
    async def scenario():
        engine=Engine(MockClient());engine.audio=Audio()
        engine.settings=Settings(mode='beat_flash',flash_trigger='percussion',flash_pattern='staggered',roll_max_hz=8)
        try:
            await engine.start();count=len(engine.client.commands)
            for i in range(8):
                now[0]=100+i*.125
                engine.audio.events.append(dict(id=i+1,at=now[0],kind='percussion',strength=1,qualified=True,roll=True,roll_interval=.125))
                await engine.apply()
                assert engine.lights[i%2].brightness==60
            assert engine.hit_flashes.flashes==8
            assert len(engine.client.commands)==count
        finally:await engine.restore()
    asyncio.run(scenario())


def test_roll_settings_validate_and_update_capture_sensitivity(monkeypatch,tmp_path):
    from app import main,config
    from app.audio.live import LiveBPM
    monkeypatch.setattr(main,'audio',LiveBPM())
    monkeypatch.setattr(config,'MOCK_MODE',True);monkeypatch.setattr(config,'MUSIC_ENABLED',False)
    monkeypatch.setattr(config,'DATA_DIR',tmp_path)
    with TestClient(main.app) as client:
        for rate in [2,9]:
            assert client.put('/api/settings',json={'roll_max_hz':rate}).status_code==422
        assert client.put('/api/settings',json={'flash_trigger':'percussion','roll_max_hz':8,'flash_sensitivity':90}).status_code==200
        assert main.audio.percussion_sensitivity==90 and not main.audio.running
        assert client.get('/api/state').json()['settings']['roll_max_hz']==8
        assert 'data-setting="roll_max_hz"' in client.get('/').text
