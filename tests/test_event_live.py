import pytest
from app.events.live import LiveEvents
from app.events.models import Cue, EventOptions
from app.events.runtime import EventRuntime


def feed(detector, at, rms=.02, bass=.01, density=2, **kwargs):
    return detector.feed(at, rms, bass, .01, density, now=at+.04,
                         track_id='live', position=at, **kwargs)


def test_live_sustained_lift_has_delay_hysteresis_and_no_duplicates():
    detector = LiveEvents()
    output = []
    for i in range(160):
        at=i*.25
        output += feed(detector, at, .02 if at<10 else .1, .01 if at<10 else .06)
    drops = [c for c in output if c.type == 'DROP']
    assert len(drops) == 1
    assert drops[0].detected_at == 10
    assert drops[0].detection_delay == pytest.approx(.79)
    assert drops[0].features['confirmation_seconds'] >= .75


def test_live_steady_kicks_and_isolated_accent_do_not_become_drops():
    for isolated in [False, True]:
        detector=LiveEvents();output=[]
        for i in range(100):
            level=.15 if isolated and i==40 else .02+(.005 if i%2 else 0)
            output += feed(detector,i*.25,level)
        assert not [c for c in output if c.type == 'DROP']


def test_live_confidence_threshold_and_stale_capture_reset():
    detector=LiveEvents();output=[]
    for i in range(60):
        output += feed(detector,i*.25,.02 if i<40 else .06,.01 if i<40 else .03,threshold=.99)
    assert not output
    assert not feed(detector,50,.15,.1)
    assert len(detector.rows)==1


def test_live_runtime_deduplication_forward_alignment_and_no_prediction():
    runtime=EventRuntime()
    runtime.options=EventOptions(enabled=True,live_trigger=True,beat_align=True)
    cue=Cue(track_id='live',type='DROP',source='live',position=10,detected_at=100,
            detection_delay=.8,confidence=.9)
    runtime.live(cue,100.8,bpm=120,phase=100)
    assert runtime.active[1] == 101  # next beat, never before confirmation
    runtime.live(cue,101,bpm=120,phase=100)
    assert len(runtime.history)==1
    assert runtime.active[0].envelope.pre_dim == 0


def test_live_final_accent_is_confirmed_after_tail_even_when_attack_precedes_window_midpoint():
    detector=LiveEvents();output=[]
    for i in range(60):
        at=i*.25
        level=.02 if at<10 else .2 if at==10 else .001
        output+=detector.feed(at,level,level/2,0,2 if at<10 else 0,now=at+.04,track_id='live',position=at)
    impacts=[c for c in output if c.type=='FINAL_IMPACT']
    assert len(impacts)==1 and impacts[0].detected_at==10
    assert impacts[0].detection_delay>=2
    assert impacts[0].features['provisional_ending']==1


def test_live_measurements_reuse_pcm_buffer_without_opening_capture():
    import numpy as np
    from app.audio.live import LiveBPM
    from app.events.live import live_measurements
    audio=LiveBPM()
    assert live_measurements(audio) is None
    audio.running=True;audio.frames=12000;audio.end_time=100
    t=np.arange(12000)/48000
    audio.buffer.append(.1*np.sin(2*np.pi*80*t))
    audio.hits.append({'kind':'percussion','at':99.9})
    end,generation,rms,bass,flux,density=live_measurements(audio)
    assert end==100 and rms>.05 and bass>0 and density>0
    assert audio.stream is None  # only an already populated memory buffer was read
