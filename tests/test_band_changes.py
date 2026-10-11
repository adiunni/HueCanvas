import numpy as np
import pytest
from scipy.signal import butter, sosfilt
from fastapi.testclient import TestClient
from app.audio.percussion import PercussionDetector, BandLead
from app.animation.beats import HitFlashes


def sounds(sr, seed=1):
    rng = np.random.default_rng(seed)
    noise_filter = butter(2, [300, min(6000, sr*.45)], btype='bandpass', fs=sr, output='sos')
    def envelope(seconds, rate):
        t = np.arange(round(seconds*sr))/sr; return t, np.exp(-rate*t)
    def kick(gain=.5):
        t, env = envelope(.25, 12); return gain*np.sin(2*np.pi*(55+90*np.exp(-30*t))*t)*env
    def snare(gain=.15):
        t, env = envelope(.12, 40); return gain*sosfilt(noise_filter, rng.normal(0, 1, len(t)))*env
    def stab(gain):
        t, env = envelope(.2, 8); return gain*sum(np.sin(2*np.pi*f*t) for f in (523, 659, 784, 1046))/4*env
    return rng, kick, snare, stab


def place(signal, sr, at, sound):
    i = round(at*sr); n = min(len(sound), len(signal)-i); signal[i:i+n] += sound[:n]


def analyze(signal, sr, block=512, bpm=None):
    detector = PercussionDetector(sr); hits = []; drops = []; leads = []
    for i in range(0, len(signal), block):
        chunk = signal[i:i+block]; end = (i+len(chunk))/sr
        detector.process(chunk, end, bpm=bpm)
        hits.extend(detector.band_hits); drops.extend(detector.drops); leads.append((end, detector.lead.band))
    return hits, drops, leads


def bass_dropout(sr):
    # Kick + backbeat, then 4 s with no bass (snare build), then kicks return.
    rng, kick, snare, _ = sounds(sr)
    t = np.arange(sr*12)/sr; signal = .02*np.sin(2*np.pi*440*t)+rng.normal(0, .002, len(t))
    for at in np.arange(.25, 12, .5):
        if not 4 <= at < 8: place(signal, sr, at, kick())
    for at in [*np.arange(.5, 4, 1), *np.arange(4, 8, .25), *np.arange(8.5, 12, 1)]:
        place(signal, sr, at, snare())
    return signal


@pytest.mark.parametrize('sr,block', [(44100, 256), (48000, 512), (48000, 4096)])
def test_bass_leads_mids_take_over_when_bass_drops_out_and_bass_returns(sr, block):
    hits, _, leads = analyze(bass_dropout(sr), sr, block)
    switches = [b for a, b in zip(leads, leads[1:]) if a[1] != b[1]]
    assert [band for _, band in switches] == ['mids', 'bass']
    # Mids take over within ~0.5 s of the first missed kick (4.25 s).
    assert 4.25 < switches[0][0] < 4.8 and 8.25 <= switches[1][0] < 9
    bass = [h for h in hits if h['band'] == 'bass']
    # One bass pulse per kick: a decaying kick does not retrigger.
    for at in [*np.arange(.25, 4, .5), *np.arange(9.25, 12, .5)]:
        assert sum(abs(h['at']-at) < .06 for h in bass) == 1
    assert not [h for h in bass if 4.2 < h['at'] < 8.2]
    mids = [h for h in hits if h['band'] == 'mids']
    assert sum(4.8 <= h['at'] < 8 for h in mids) >= 12
    assert all(h['kind'] == 'band' and h['qualified'] for h in hits)


@pytest.mark.parametrize('bassless', [False, True])
def test_drop_band_follows_whether_the_drop_brings_bass(bassless):
    sr = 48000; rng, kick, snare, stab = sounds(sr)
    signal = rng.normal(0, .002, sr*8)
    for at in np.arange(.25, 4, .5): place(signal, sr, at, kick(.12))
    for at in np.arange(.5, 4, 1): place(signal, sr, at, snare(.04))
    for at in np.arange(4, 8, .25):
        if bassless:
            place(signal, sr, at, snare(.2)); place(signal, sr, at+.125, stab(.3))
        else:
            if at % .5 == 0: place(signal, sr, at, kick(.5))
            if at % 1 == .5: place(signal, sr, at, snare(.2))
    hits, drops, leads = analyze(signal, sr, bpm=120)
    band = 'mids' if bassless else 'bass'
    assert len(drops) == 1 and 3.95 <= drops[0]['at'] < 4.6 and drops[0]['band'] == band
    after = [h for h in hits if h['at'] > drops[0]['confirmed_at']]
    assert len(after) >= (12 if bassless else 7) and all(h['band'] == band for h in after)
    if bassless:
        # Mids follow the snares; the synth stabs between them are notes.
        assert all(min(abs(h['at'] - at) for at in np.arange(4, 8, .25)) < .03 for h in after)
    assert all(lead == band for at, lead in leads if at > drops[0]['confirmed_at']+.05)


def test_band_lead_hold_and_drop_lock_prevent_flicker():
    lead = BandLead(); at = 0
    for _ in range(4):
        at += .5; lead.update(at, True, True, 1, 1)
    assert lead.band == 'bass'
    # Mids alone cannot take over until the next bass attack is clearly missed.
    lead.update(at+.5, False, True, 1, 1)
    assert lead.band == 'bass'
    assert lead.update(at+.81, False, True, 1, 1) == 'mids'
    # A bass-less drop locks mids even if a stray low attack appears.
    lead.force('mids', at+1)
    for step in (1.1, 1.3, 1.5): lead.update(at+step, True, False, 1, 1)
    assert lead.band == 'mids'
    # After the lock, bass needs two fresh attacks to reclaim the lead.
    assert lead.update(at+3.1, True, False, 1, 1) == 'mids'
    assert lead.update(at+3.6, True, False, 1, 1) == 'bass'
    # Low-band attacks far below the mids are treated as leakage.
    quiet = BandLead()
    for step in range(400): quiet.update(step*.01, step % 50 == 0, step % 25 == 0, .01, 1)
    assert quiet.band == 'mids'


def band_hits(band='mids', rate=8, seconds=2, roll=False):
    return [dict(id=i+1, at=1+i/rate, kind='band', band=band, strength=2, qualified=True,
                 roll=roll, roll_interval=1/rate if roll else None) for i in range(round(rate*seconds))]


def flashes(events, **kwargs):
    effect = HitFlashes(); starts = []
    for now in np.arange(1, 3.3, .05):
        before = effect.flashes
        effect.factors(events, now, 2, 'bands', 'together', **kwargs)
        if effect.flashes > before: starts.append(effect.last_flash)
    return starts


def test_bands_trigger_uses_band_hits_only_and_allows_roll_rate():
    others = [dict(id=100+i, at=1+i/4, kind=kind, strength=9, qualified=True)
              for i, kind in enumerate(['percussion', 'bass', 'frequency_mids'])]
    assert flashes(others) == []
    ordinary = flashes(band_hits())
    assert len(ordinary) <= 7 and min(np.diff(ordinary)) >= 1/3-1e-9
    assert len(flashes(band_hits(roll=True), roll_max_hz=8)) == 16


def test_bands_trigger_setting_is_accepted(monkeypatch, tmp_path):
    from app import main, config
    from app.audio.live import LiveBPM
    monkeypatch.setattr(main, 'audio', LiveBPM())
    monkeypatch.setattr(config, 'MOCK_MODE', True); monkeypatch.setattr(config, 'MUSIC_ENABLED', False)
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
    with TestClient(main.app) as client:
        assert client.put('/api/settings', json={'flash_trigger': 'bands'}).status_code == 200
        assert client.get('/api/state').json()['settings']['flash_trigger'] == 'bands'
        assert 'value="bands"' in client.get('/').text
