import asyncio
import pytest
from app.animation.beats import BeatLock
from app.animation.engine import Engine
from app.hue.mock import MockClient
from app.hue.models import Settings


def kicks(bpm,start,count,offset=0,family='kick',skip=()):
    period=60/bpm
    return [{'id':i+1,'at':start+offset+i*period,'kind':'percussion','family':family,'strength':5,'qualified':True}
            for i in range(count) if i not in skip]


def run(lock,hits,bpm,phase_at,start,end,fps=20):
    """Feed frames like the 20 fps stream; returns (time, beat time, period) per frame."""
    frames=[];t=start
    while t<end:
        clock_bpm,anchor=lock.update([h for h in hits if h['id']>lock.cursor],t,bpm,phase_at)
        frames.append((t,anchor,60/clock_bpm if clock_bpm else None));t+=1/fps
    return frames


def error(anchor,period,at):
    beats=round((at-anchor)/period);return at-(anchor+beats*period)


def drift(frame,grid,true_period):
    """Clock error against the drums' beat grid at the frame's own time (not extrapolated)."""
    t,anchor,period=frame
    return error(anchor,period,grid+round((t-grid)/true_period)*true_period)


def test_clock_converges_onto_kicks_without_jumping():
    # Detector phase 60 ms late at 128 BPM; the real kicks land on the true beat.
    lock=BeatLock();hits=kicks(128,100,40)
    frames=run(lock,hits,128,100.06,100,100+40*60/128)
    steps=[abs(b[1]-a[1]) for a,b in zip(frames,frames[1:])]
    assert max(steps)<.025  # each correction is a fraction of the error, never a jump
    t,anchor,period=frames[-1]
    assert abs(error(anchor,period,hits[-1]['at']))<.005
    # Within six kicks the clock is already on the drums.
    assert abs(drift(min(frames,key=lambda f:abs(f[0]-(hits[6]['at']+.01))),100,60/128))<.004


def test_pulses_continue_through_missing_kicks():
    lock=BeatLock();period=60/128
    hits=kicks(128,100,48,skip=range(8,40))  # 32 beats with no detected kick (fill/breakdown)
    frames=run(lock,hits,128,100.03,100,100+48*period)
    # The clock never stops or stretches: every frame has a beat period near 128 BPM.
    assert all(f[1] is not None and abs(f[2]-period)<period*.031 for f in frames)
    t,anchor,p=frames[-1];assert abs(error(anchor,p,hits[-1]['at']))<.01
    # Right before the kicks return, the free-running clock is still on the beat.
    t,anchor,p=min(frames,key=lambda f:abs(f[0]-(100+40*period-.01)))
    assert abs(error(anchor,p,100+40*period))<.02


def test_hats_and_off_beat_snares_do_not_move_the_clock():
    lock=BeatLock();period=60/120
    hits=kicks(120,100,16,offset=period/2,family='snare')+[dict(h,id=h['id']+100) for h in kicks(120,100,16,offset=.01,family='hat')]
    hits.sort(key=lambda h:h['at'])
    frames=run(lock,hits,120,100,100,108)
    assert all(abs(error(f[1],period,100))<1e-6 for f in frames)


def test_kicks_consistently_half_a_beat_off_realign_the_clock():
    # The detector picked the off-beat; four agreeing kicks move the clock onto them.
    lock=BeatLock();period=60/124
    frames=run(lock,kicks(124,100,12),124,100+period/2,100,100+12*period)
    t,anchor,p=frames[-1]
    assert abs(error(anchor,p,100))<.01


def test_tempo_trim_follows_a_slightly_wrong_detector_tempo():
    # Detector reads 127.4 BPM; the record plays at 128.
    lock=BeatLock()
    frames=run(lock,kicks(128,100,96),127.4,100,100,100+96*60/128)
    t,anchor,p=frames[-1]
    assert abs(error(anchor,p,100+95*60/128))<.012 and p==pytest.approx(60/128,rel=.004)


def test_without_drums_the_clock_keeps_the_drum_phase_and_follows_detector_moves():
    lock=BeatLock();period=60/120
    run(lock,kicks(120,100,16,offset=.05),120,100,100,108)  # kicks land 50 ms after the detector's beat
    frames=run(lock,[],120,100,108,120)  # breakdown: no drums, detector unchanged
    assert abs(drift(frames[-1],100.05,period))<.006  # still on the drums' phase
    frames=run(lock,[],120,100.04,120,132)  # the DJ nudges the record 40 ms later
    assert abs(drift(frames[-1],100.09,period))<.008  # follows the nudge, keeps the offset


def test_new_tempo_or_lost_tempo_reseeds_from_the_detector():
    lock=BeatLock()
    run(lock,kicks(120,100,8,offset=.05),120,100,100,104)
    bpm,anchor=lock.update([],104,140,104.2)
    assert bpm==140 and anchor==104.2
    assert lock.update([],105,None,None)==(None,None)
    assert lock.update([],106,140,105.9)==(140,105.9)


def engine_with(audio,monkeypatch,**settings):
    import app.animation.engine as module
    now=[100.];monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    engine=Engine(MockClient());engine.audio=audio
    engine.settings=Settings(mode='beat_flash',bpm_source='audio',flash_trigger='beat_drums',pulse_palette='fixed',**settings)
    return engine,now


def test_engine_beat_drums_pulses_on_every_beat_with_no_hits(monkeypatch):
    class Audio:
        running=True;phase_at=100;hit_counter=0
        def current_bpm(self):return 120
        def music_energy(self):return .02
        def recent_hits(self,after=0):return []
    engine,now=engine_with(Audio(),monkeypatch)
    async def scenario():
        await engine.start()
        seen=[]
        for step in range(40):  # two seconds at 20 fps
            now[0]=100+step*.05;await engine.apply();seen.append(engine.lights[0].brightness)
        flashes=sum(1 for a,b in zip(seen,seen[1:]) if b>a)
        assert flashes==3 and seen[0]==60  # beats at 100.5, 101, 101.5 after the first
        await engine.stop()
    asyncio.run(scenario())


def test_engine_beat_drums_follows_hits_until_tempo_locks_and_adds_rolls(monkeypatch):
    hits=[{'id':1,'at':100.,'kind':'percussion','family':'snare','strength':5,'qualified':True,'roll':False}]
    class Audio:
        running=True;phase_at=None;hit_counter=0;bpm=None
        def current_bpm(self):return self.bpm
        def music_energy(self):return .02
        def recent_hits(self,after=0):return [h for h in hits if h['id']>after]
    audio=Audio();engine,now=engine_with(audio,monkeypatch)
    async def scenario():
        assert engine.pulse_trigger()=='percussion'
        await engine.start()
        assert engine.lights[0].brightness==60  # the first drum hit flashes before any tempo
        audio.bpm=120;audio.phase_at=100;assert engine.pulse_trigger()=='beat_drums'
        # A confirmed roll hit between beats adds a flash on top of the clock.
        hits.append({'id':2,'at':100.25,'kind':'percussion','family':'snare','strength':5,'qualified':True,'roll':True,'roll_interval':.125})
        now[0]=100.25;await engine.apply()
        assert engine.lights[0].brightness==60
        now[0]=100.42;await engine.apply()
        assert engine.lights[0].brightness==12
        await engine.stop()
    asyncio.run(scenario())
