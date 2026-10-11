import numpy as np
import pytest
from scipy.signal import butter, sosfilt
from fastapi.testclient import TestClient
from app.audio.percussion import PercussionDetector
from app.animation.beats import HitFlashes

SR = 48000
rng = np.random.default_rng(5)


def midi(note): return 440 * 2 ** ((note - 69) / 12)


def place(signal, at, sound):
    i = round(at * SR); n = min(len(sound), len(signal) - i); signal[i:i + n] += sound[:n]


def saw(frequency, seconds, harmonics=20):
    t = np.arange(round(seconds * SR)) / SR
    return sum(np.sin(2 * np.pi * h * frequency * t) / h for h in range(1, harmonics + 1) if h * frequency < SR * .45)


def lowpass(x, cutoff): return sosfilt(butter(2, cutoff, fs=SR, output='sos'), x)


def envelope(seconds, attack, decay):
    t = np.arange(round(seconds * SR)) / SR
    return np.minimum(1, t / attack) * np.exp(-decay * t) * np.minimum(1, (seconds - t) / .01)


def kick():
    t = np.arange(round(.35 * SR)) / SR
    click = sosfilt(butter(2, [1500, 6000], btype='bandpass', fs=SR, output='sos'), rng.normal(0, 1, len(t)))
    return .6 * (np.sin(2 * np.pi * np.cumsum(50 + 110 * np.exp(-35 * t)) / SR) * np.exp(-9 * t) + .5 * click * np.exp(-300 * t))


def snare():
    t = np.arange(round(.2 * SR)) / SR
    noise = sosfilt(butter(2, [400, 9000], btype='bandpass', fs=SR, output='sos'), rng.normal(0, 1, len(t)))
    return .25 * (noise * np.exp(-30 * t) + .5 * np.sin(2 * np.pi * 190 * t) * np.exp(-25 * t))


def hat():
    t = np.arange(round(.15 * SR)) / SR
    return .08 * sosfilt(butter(4, 7000, btype='highpass', fs=SR, output='sos'), rng.normal(0, 1, len(t))) * np.exp(-70 * t)


def notes(kind, seconds=6):
    signal = rng.normal(0, .001, seconds * SR)
    if kind == 'piano':
        for i, at in enumerate(np.arange(.3, seconds - .5, .375)):
            t = np.arange(round(1.2 * SR)) / SR; f = midi([60, 64, 67, 72][i % 4])
            place(signal, at, .18 * sum(np.sin(2 * np.pi * f * k * t) / k for k in range(1, 11)) * envelope(1.2, .002, 3.5))
    elif kind == 'pluck':
        for i, at in enumerate(np.arange(.3, seconds - .3, .25)):
            place(signal, at, .25 * lowpass(saw(midi([69, 72, 76, 74][i % 4]), .4), 3000) * envelope(.4, .001, 9))
    elif kind in ('bass', 'legato'):
        legato = kind == 'legato'
        for i, at in enumerate(np.arange(.3, seconds - .3, .25)):
            length = .27 if legato else .22
            place(signal, at, .45 * lowpass(saw(midi([33, 33, 45, 36][i % 4]), length, 30), 600) * envelope(length, .015 if legato else .004, 2 if legato else 7))
    elif kind == 'sub':
        for i, at in enumerate(np.arange(.3, seconds - .3, .5)):
            t = np.arange(round(.48 * SR)) / SR
            place(signal, at, .5 * np.sin(2 * np.pi * midi([33, 36, 31, 38][i % 4]) * t) * envelope(.48, .006, 1.5))
    elif kind == 'voice':
        for i, at in enumerate(np.arange(.3, seconds - .6, .6)):
            t = np.arange(round(.55 * SR)) / SR
            f = midi([64, 67, 69][i % 3]) * (1 + .012 * np.sin(2 * np.pi * 5.5 * t))
            tone = sum(np.sin(2 * np.pi * h * np.cumsum(f) / SR) / h for h in range(1, 15))
            place(signal, at, .1 * tone * np.minimum(1, t / .04) * np.minimum(1, (.55 - t) / .05))
    return signal


def groove(seconds=6):
    signal = np.zeros(seconds * SR); events = []
    for at in np.arange(.3, seconds - .3, .5): place(signal, at, kick()); events.append(('kick', at))
    for at in np.arange(.55, seconds - .3, 1.): place(signal, at, snare()); events.append(('snare', at))
    for at in np.arange(.425, seconds - .3, .25): place(signal, at, hat()); events.append(('hat', at))
    return signal, events


def analyze(signal, block=512, isolate=True):
    detector = PercussionDetector(SR); hits = []; bands = []; delays = []
    for i in range(0, len(signal), block):
        chunk = signal[i:i + block]; end = (i + len(chunk)) / SR
        found = detector.process(chunk, end, isolate=isolate)
        hits += found; bands += detector.band_hits; delays += [end - h['at'] for h in found]
    return hits, bands, delays


@pytest.mark.parametrize('kind', ['piano', 'pluck', 'bass', 'legato', 'sub', 'voice'])
def test_notes_alone_do_not_trigger_drum_or_band_pulses(kind):
    signal = notes(kind)
    hits, bands, _ = analyze(signal)
    assert hits == [] and bands == []
    # The fast path still reacts to note attacks; isolation is what removes them.
    if kind != 'voice': assert analyze(signal, isolate=False)[0]


def test_drums_are_found_labelled_and_timed_within_the_stem_delay():
    signal, events = groove()
    hits, _, delays = analyze(signal)
    for kind, at in events:
        near = [h for h in hits if abs(h['at'] - at) < .03]
        assert len(near) == 1, (kind, at)
        if kind == 'kick': assert near[0]['family'] == 'kick'
        else: assert near[0]['family'] != 'kick'
    assert len(hits) == len(events)
    # Timestamps stay on the attack; confirmation arrives 40–70 ms later.
    assert .035 <= min(delays) and max(delays) <= .07


@pytest.mark.parametrize('kind', ['bass', 'piano', 'pluck'])
def test_drums_over_notes_keep_drums_and_drop_the_notes(kind):
    drums, events = groove()
    hits, _, _ = analyze(drums + notes(kind))
    matched = [h for h in hits if min(abs(h['at'] - at) for _, at in events) < .03]
    assert len(matched) == len(hits)  # no pulse lands on a note alone
    assert len(matched) >= .75 * len(events)
    kicks = [at for k, at in events if k == 'kick']
    assert sum(any(abs(h['at'] - at) < .03 for h in hits) for at in kicks) >= .6 * len(kicks)


def test_sharp_flash_confirmed_late_keeps_its_full_width():
    effect = HitFlashes(); hit = dict(id=1, at=1., kind='percussion', strength=2, qualified=True)
    assert effect.factors([hit], 1.06, 2, 'percussion', 'together') == [1, 1]
    assert effect.factors([], 1.15, 2, 'percussion', 'together') == [1, 1]
    assert effect.factors([], 1.17, 2, 'percussion', 'together') == [.2, .2]
    # Spacing still follows the attack time, not the confirmation time.
    assert effect.last_flash == 1


def test_drum_isolation_setting_reaches_live_capture(monkeypatch, tmp_path):
    from app import main, config
    from app.audio.live import LiveBPM
    monkeypatch.setattr(main, 'audio', LiveBPM())
    monkeypatch.setattr(config, 'MOCK_MODE', True); monkeypatch.setattr(config, 'MUSIC_ENABLED', False)
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
    with TestClient(main.app) as client:
        assert main.audio.drum_isolation is True
        assert client.put('/api/settings', json={'drum_isolation': 'fast'}).status_code == 200
        assert main.audio.drum_isolation is False
        assert client.put('/api/settings', json={'drum_isolation': 'neural'}).status_code == 422
        assert 'data-setting="drum_isolation"' in client.get('/').text
