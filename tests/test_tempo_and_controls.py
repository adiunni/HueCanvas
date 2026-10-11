import asyncio
import numpy as np
import pytest
from scipy.signal import butter, sosfilt
from fastapi.testclient import TestClient
from app.audio.bpm import detect_bpm, TempoTracker
from app.animation.engine import Engine
from app.hue.models import Settings
from app.hue.mock import MockClient

SR = 24000
rng = np.random.default_rng(3)


def hit(kind):
    t = np.arange(round(.3 * SR)) / SR
    if kind == 'kick':
        return .6 * np.sin(2 * np.pi * np.cumsum(50 + 110 * np.exp(-35 * t)) / SR) * np.exp(-9 * t)
    if kind == 'snare':
        noise = sosfilt(butter(2, [400, 9000], btype='bandpass', fs=SR, output='sos'), rng.normal(0, 1, len(t)))
        return .25 * noise * np.exp(-30 * t)
    return .06 * sosfilt(butter(4, 7000, btype='highpass', fs=SR, output='sos'), rng.normal(0, 1, len(t))) * np.exp(-70 * t)


# 16th-note positions per bar: kick, snare, hat
GENRES = {'house': (124, [0, 4, 8, 12], [4, 12], range(0, 16)),
          'hiphop': (90, [0, 7, 10], [4, 12], range(0, 16, 2)),
          'dubstep': (140, [0], [8], range(0, 16, 2)),
          'trap': (145, [0, 11], [8], range(0, 16)),
          'reggaeton': (96, [0, 4, 8, 12], [3, 6, 11, 14], range(0, 16, 2))}


def groove(name, seconds=10):
    bpm, kicks, snares, hats = GENRES[name]
    step = 60 / bpm / 4; signal = rng.normal(0, .002, seconds * SR)
    for index in range(int(seconds / step)):
        at = .1 + index * step; position = index % 16
        for kind, steps in (('kick', kicks), ('snare', snares), ('hat', hats)):
            if position in steps:
                start = round(at * SR); sound = hit(kind)[:len(signal) - start]; signal[start:start + len(sound)] += sound
    return signal


@pytest.mark.parametrize('name', GENRES)
def test_dj_tempo_from_eight_seconds_including_half_time_genres(name):
    # Half-time genres (dubstep, trap) read at their DJ tempo, not half;
    # syncopated hip-hop and reggaeton avoid 3:2 and 4:5 aliases.
    result = detect_bpm(groove(name, 8), SR)
    assert result and abs(result['bpm'] - GENRES[name][0]) <= 1


def test_tempo_tracker_locks_snaps_and_ignores_single_octave_flips():
    tracker = TempoTracker()
    for estimate in (127.9, 128.1, 128.05):
        tracker.update(estimate)
    assert tracker.locked and tracker.bpm == 128
    assert tracker.update(64.0) == 128 and tracker.update(128.0) == 128
    for estimate in (100.0, 100.2, 99.9):
        tracker.update(estimate)
    # A new song's tempo replaces the old one after three agreeing windows.
    assert abs(tracker.bpm - 100) < .3
    tracker.reset()
    assert tracker.bpm is None and not tracker.locked


def test_tempo_scale_halves_or_doubles_until_the_track_changes(monkeypatch, tmp_path):
    from app import main, config
    from app.audio.live import LiveBPM
    audio = LiveBPM(); audio.running = True; audio.bpm = 87.; audio.last_good_at = float('inf')
    monkeypatch.setattr(main, 'audio', audio)
    monkeypatch.setattr(config, 'MOCK_MODE', True); monkeypatch.setattr(config, 'MUSIC_ENABLED', False)
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
    with TestClient(main.app) as client:
        assert client.post('/api/audio/tempo', json={'factor': 2}).json()['bpm'] == 174
        assert client.post('/api/audio/tempo', json={'factor': 3}).status_code == 422
        audio.track_changed()
        assert audio.tempo_scale == 1
        audio.running = False


class Audio:
    running = True; phase_at = 100.; hit_counter = 0; bpm = None
    def music_energy(self): return .05
    def current_bpm(self): return self.bpm
    def recent_hits(self, after=0): return []


def test_beat_clock_follows_drums_until_the_tempo_locks():
    engine = Engine(MockClient()); engine.audio = Audio()
    engine.settings = Settings(mode='beat_flash', flash_trigger='bpm')
    assert engine.settings.bpm_source == 'audio' and engine.pulse_trigger() == 'percussion'
    engine.audio.bpm = 128
    assert engine.pulse_trigger() == 'bpm'
    engine.settings = Settings(mode='beat_flash', flash_trigger='bpm', bpm_source='manual')
    engine.audio.bpm = None
    assert engine.pulse_trigger() == 'bpm'


def test_white_lights_use_their_own_floor_and_pattern(monkeypatch):
    import app.animation.engine as module
    monkeypatch.setattr(module.time, 'monotonic', lambda: 100.)
    async def scenario():
        engine = Engine(MockClient()); await engine.refresh()
        for light in engine.lights: light.enabled = True
        engine.audio = Audio(); engine.audio.bpm = 120
        engine.music_factor = lambda now: .2  # colour envelope at its 20% floor
        engine.settings = Settings(mode='beat_flash', flash_low=20, white_floor=50, white_fill=40,
                                   flash_pattern='staggered', white_pattern='together')
        await engine.rest_music(engine.lights, force=True)
        whites = [light.brightness for light in engine.lights if not light.color]
        assert whites == [20, 20]  # 50% of 40, not the colour bulbs' 20% floor
    asyncio.run(scenario())


def test_start_listens_only_for_music_looks(monkeypatch, tmp_path):
    from app import main, config
    class Fake:
        running = False; device_id = None; hit_counter = 0; phase_at = None
        def current_bpm(self): return None
        def music_energy(self): return None
        def recent_hits(self, after=0): return []
        def status(self): return {'running': self.running}
        async def start(self, device_id): self.running = True; self.device_id = device_id
        async def stop(self): self.running = False
    fake = Fake()
    monkeypatch.setattr(main, 'audio', fake)
    monkeypatch.setattr(main, 'input_devices', lambda: [{'id': 4, 'name': 'BlackHole 2ch', 'loopback': True}])
    monkeypatch.setattr(config, 'MOCK_MODE', True); monkeypatch.setattr(config, 'MUSIC_ENABLED', False)
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
    with TestClient(main.app) as client:
        client.put('/api/settings', json={'mode': 'crossfade', 'white_response': 'music'})
        assert client.post('/api/control/start').status_code == 200 and not fake.running
        client.put('/api/settings', json={'mode': 'beat_gentle', 'flash_trigger': 'percussion'})
        assert client.post('/api/control/start').status_code == 200 and fake.running and fake.device_id == 4
        assert client.post('/api/control/restore').status_code == 200 and not fake.running


def test_tempo_range_reaches_512_for_sixteenth_note_loop_rolls(monkeypatch, tmp_path):
    from app import main, config
    from app.audio.live import LiveBPM
    assert Settings(bpm=512).bpm == 512
    with pytest.raises(ValueError): Settings(bpm=513)
    audio = LiveBPM(); audio.running = True; audio.bpm = 128.; audio.last_good_at = float('inf')
    monkeypatch.setattr(main, 'audio', audio)
    monkeypatch.setattr(config, 'MOCK_MODE', True); monkeypatch.setattr(config, 'MUSIC_ENABLED', False)
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
    with TestClient(main.app) as client:
        assert client.put('/api/settings', json={'bpm_source': 'manual', 'bpm': 480}).status_code == 200
        client.post('/api/audio/tempo', json={'factor': 2})
        assert client.post('/api/audio/tempo', json={'factor': 2}).json()['bpm'] == 512  # 128 × 4
        response = client.post('/api/audio/tempo', json={'factor': 2})
        assert response.status_code == 400 and '512' in response.json()['detail']
        assert 'max="512"' in client.get('/').text
        audio.running = False


def test_fast_tempos_keep_flash_and_pulse_rate_limits():
    from app.animation.beats import clock_flash_factors, clock_gentle_factors
    def peaks(factors_at, seconds=4, fps=200):
        values = [factors_at(i / fps)[0] for i in range(seconds * fps)]
        return sum(1 for a, b in zip(values, values[1:]) if b == 1 and a < 1)
    for bpm in (240, 480, 512):
        assert peaks(lambda t: clock_flash_factors(bpm, t, 0, 2, 'together')) <= 3 * 4 + 1
        swells = [clock_gentle_factors(bpm, t / 200, 0, 1, 'together')[0] for t in range(800)]
        tops = sum(1 for a, b, c in zip(swells, swells[1:], swells[2:]) if b > a and b >= c and b > .95)
        assert tops <= 4 * 4 + 1


def test_loop_roll_keeps_the_track_tempo_like_dj_software():
    # A sustained 1/16-note roll on a 128 BPM track is 512 hits per minute;
    # the displayed tempo stays 128 so the beat grid does not jump.
    signal = rng.normal(0, .002, 10 * SR)
    for at in np.arange(.1, 10, 60 / 128 / 4):
        start = round(at * SR); sound = (hit('kick') + hit('snare'))[:len(signal) - start]; signal[start:start + len(sound)] += sound
    assert abs(detect_bpm(signal, SR)['bpm'] - 128) <= 1
