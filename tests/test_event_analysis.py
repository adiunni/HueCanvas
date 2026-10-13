from io import BytesIO
import numpy as np
import pytest
from scipy.io import wavfile
from app.events.analysis import features, detect, read_wav, analyze_wav

SR = 12000


def kick(signal, at, gain=.3):
    t = np.arange(int(SR * .18)) / SR
    hit = gain * np.sin(2 * np.pi * (70 * t + 30 * .02 * (1 - np.exp(-t / .02)))) * np.exp(-t / .045)
    start = round(at * SR)
    signal[start:start + len(hit)] += hit[:len(signal) - start]


def fixture(kind):
    rng = np.random.default_rng(4)
    t = np.arange(SR * 32) / SR
    signal = .018 * np.sin(2 * np.pi * 430 * t)
    if kind == 'drop':
        signal *= np.where(t < 12, .4 + .6 * t / 12, 3)
        for at in np.arange(.5, 32, .5):kick(signal, at, .04 if at < 12 else .5)
    else:
        for at in np.arange(.5, 32, .5):kick(signal, at)
    if kind in ('final', 'repeated_final'):
        signal[t >= 23] = 0
        for at in ([25] if kind == 'final' else [24, 25, 26]):
            start = round(at * SR)
            tail_t = t[start:] - at
            tail = (.8 * np.sin(2 * np.pi * 90 * tail_t) + .2 * rng.standard_normal(len(tail_t))) * np.exp(-tail_t / .7)
            signal[start:] += tail
    elif kind == 'fade':
        signal *= np.clip((32 - t) / 12, 0, 1)
    elif kind == 'abrupt':
        signal[t >= 24] = 0
    elif kind == 'outro':
        signal[t >= 22] -= .018 * np.sin(2 * np.pi * 430 * t[t >= 22])
    return signal.astype(np.float32)


def events(kind):
    signal = fixture(kind)
    analysis = features(signal, SR)
    return detect(kind, analysis), analysis


def test_quiet_build_and_sustained_drop_not_every_kick():
    cues, analysis = events('drop')
    drops = [c for c in cues if c.type == 'DROP']
    assert len(drops) == 1
    assert drops[0].position == pytest.approx(12, abs=.2)
    assert drops[0].confidence >= .7
    assert drops[0].features['bass_return'] > 1.5
    assert analysis['beat_candidates'] and analysis['downbeat_candidates']
    assert all(np.isfinite(value).all() for value in analysis['normalized'].values())


@pytest.mark.parametrize('kind', ['kicks', 'fade', 'outro', 'abrupt'])
def test_kicks_fades_and_dj_outros_have_no_distinct_final_impact(kind):
    cues, _ = events(kind)
    assert not [c for c in cues if c.type in ('DROP', 'FINAL_IMPACT')]


@pytest.mark.parametrize('kind,at', [('final', 25), ('repeated_final', 26)])
def test_final_accent_with_tail_selects_last_supported_hit(kind, at):
    cues, analysis = events(kind)
    final = [c for c in cues if c.type == 'FINAL_IMPACT']
    assert len(final) == 1
    assert final[0].position == pytest.approx(at, abs=.2)
    assert final[0].features['tail_ratio'] < .32
    assert analysis['audible_end'] > at + 2


def test_abrupt_silence_is_track_end_not_an_invented_final_hit():
    cues, _ = events('abrupt')
    assert next(c for c in cues if c.type == 'TRACK_END').position == pytest.approx(24, abs=.15)


def test_wav_formats_opposite_polarity_and_no_audio_in_analysis():
    signal = fixture('final')
    out = BytesIO()
    wavfile.write(out, SR, (np.stack([signal, -signal], axis=1) * 32767).astype(np.int16))
    decoded, rate = read_wav(out.getvalue())
    assert rate == SR and np.max(abs(decoded)) > .1
    cues, analysis = analyze_wav(out.getvalue(), 'authorized-file')
    assert any(c.type == 'FINAL_IMPACT' for c in cues)
    assert analysis['audio_sha256'] and 'samples' not in analysis
    with pytest.raises(ValueError, match='WAV'):
        read_wav(b'not audio')


def test_silence_has_no_musical_events():
    assert detect('silence', features(np.zeros(SR * 5), SR)) == []


def test_gradual_build_produces_start_and_end_candidates():
    t=np.arange(SR*30)/SR
    signal=.02*np.sin(2*np.pi*400*t)
    gain=np.where(t<14,.2+.8*np.clip((t-4)/10,0,1),3.)
    for at in np.arange(.5,30,.5):kick(signal,at,.15)
    cues=detect('build',features(signal*gain,SR))
    assert any(c.type=='BUILD_START' and c.position<14 for c in cues)
    assert any(c.type=='BUILD_END' and abs(c.position-14)<.2 for c in cues)


def test_final_hit_is_not_at_a_fixed_offset_from_file_end():
    signal=fixture('final')
    padded=np.concatenate([signal,np.zeros(SR*40)])
    cues=detect('long-tail-file',features(padded,SR))
    assert next(c for c in cues if c.type=='FINAL_IMPACT').position==pytest.approx(25,abs=.2)


def test_reference_workflow_rejects_missing_or_wrong_recording_and_null_times():
    import hashlib
    from examples.check_reference_events import evaluate
    out=BytesIO();wavfile.write(out,SR,fixture('final'))
    data=out.getvalue()
    with pytest.raises(ValueError,match='SHA-256'):evaluate(data,{'schema_version':1})
    annotation={'schema_version':1,'audio_sha256':hashlib.sha256(data).hexdigest(),
                'duration_seconds':32,'events':[{'type':'FINAL_IMPACT','position_seconds':None}]}
    with pytest.raises(ValueError,match='Annotate'):evaluate(data,annotation)
    annotation['events'][0]['position_seconds']=25
    report=evaluate(data,annotation)
    assert report['matches'][0]['matched']
