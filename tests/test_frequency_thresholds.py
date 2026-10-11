import asyncio
import time
import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.audio.frequency import FrequencyThresholds, FrequencyDetector
from app.audio.spectrum import WINDOW_SIZE, analyze_spectrum
from app.audio.live import LiveBPM
from app.animation.beats import HitFlashes
from app.animation.engine import Engine
from app.hue.models import Settings
from app.hue.mock import MockClient
from tests.test_audio_reliability import fake_input


@pytest.mark.parametrize('bass,mids,kind', [
    (-14, -1, 'frequency_bass'), (-80, -19, 'frequency_mids'),
    (-30, -1, None), (-70, -1, None), (-15, -1, None), (-80, -20, None), (-90, -90, None),
])
def test_exact_threshold_comparisons_and_bass_priority(bass, mids, kind):
    gate = FrequencyThresholds()
    hits = gate.update(bass, mids, 1)
    assert [hit['kind'] for hit in hits] == ([kind] if kind else [])
    if hits: assert hits[0]['at'] == 1


def test_sustained_audio_and_small_boundary_wobbles_do_not_retrigger():
    gate = FrequencyThresholds()
    assert len(gate.update(-14, -90, 0)) == 1
    for i, bass in enumerate([-14, -15.5, -16.9, -14, -14]):
        assert gate.update(bass, -90, i + 1) == []
    assert gate.update(-17, -90, 6) == []
    assert len(gate.update(-14, -90, 7)) == 1
    assert gate.update(-14, -90, 8) == []


def test_blocked_mids_can_trigger_on_handover_but_do_not_repeat_without_rearming():
    gate = FrequencyThresholds()
    assert gate.update(-30, -10, 1) == []
    assert gate.update(-80, -10, 2)[0]['kind'] == 'frequency_mids'
    assert gate.update(-30, -10, 3) == []
    assert gate.update(-80, -10, 4) == []
    assert gate.update(-80, -22, 5) == []
    assert gate.update(-80, -10, 6)[0]['kind'] == 'frequency_mids'


@pytest.mark.parametrize('sr', [12000, 44100, 48000])
def test_trigger_levels_match_calibrated_spectrum_band_rms(sr):
    length = max(WINDOW_SIZE * 2, sr)
    samples = .35 * np.sin(2 * np.pi * 1000 * np.arange(length) / sr)
    detector = FrequencyDetector(sr)
    size = len(samples) // detector.hop * detector.hop
    hits = detector.process(samples[:size], size / sr)
    spectrum = analyze_spectrum(samples[size - WINDOW_SIZE:size], sr)
    assert [hit['kind'] for hit in hits] == ['frequency_mids']
    for band in spectrum['bands']:
        assert detector.gate.levels[band['id']] == pytest.approx(band['db'], abs=.06)


def mixed_signal(sr=48000):
    t = np.arange(sr * 3) / sr
    signal = np.zeros(len(t))
    for begin, end, frequency, amplitude in [(.3,.6,100,.5), (.9,1.2,100,.5), (1.7,2.3,1000,.3)]:
        selected = (t >= begin) & (t < end)
        signal[selected] = amplitude * np.sin(2 * np.pi * frequency * t[selected])
    return signal


def analyze_chunks(samples, chunk_size):
    detector = FrequencyDetector(48000)
    hits = []
    for start in range(0, len(samples), chunk_size):
        chunk = samples[start:start + chunk_size]
        hits.extend(detector.process(chunk, (start + len(chunk)) / 48000))
    return hits


def test_bass_then_mids_audio_and_timestamps_are_independent_of_callback_chunks():
    small = analyze_chunks(mixed_signal(), 257)
    large = analyze_chunks(mixed_signal(), 4096)
    assert [hit['kind'] for hit in small] == ['frequency_bass', 'frequency_bass', 'frequency_mids']
    assert [hit['at'] for hit in small] == pytest.approx([hit['at'] for hit in large])
    # The shared FFT window smooths attacks, unlike the shorter percussion onset window.
    assert .3 < small[0]['at'] < .5
    assert 1.7 < small[2]['at'] < 2
    assert analyze_chunks(np.zeros(48000), 257) == []


@pytest.mark.parametrize('mode', ['beat_flash', 'beat_gentle'])
def test_threshold_events_bypass_relative_hit_sensitivity_and_keep_stagger_and_rate_cap(mode):
    effect = HitFlashes()
    event = dict(id=1, at=1., kind='frequency_bass', strength=1., db=-14)
    assert effect.factors([event], 1, 2, 'frequency', 'staggered', sensitivity=1, mode=mode) == [1, .2]
    # A strong percussion event must not bypass the selected frequency policy.
    event2 = dict(id=2, at=2., kind='percussion', strength=100)
    assert effect.factors([event2], 2, 2, 'frequency', 'staggered', mode=mode) == [.2, .2]
    event3 = dict(id=3, at=2.5, kind='frequency_mids', strength=1., db=-19)
    assert effect.factors([event3], 2.5, 2, 'frequency', 'staggered', mode=mode) == [.2, 1]
    effect.reset(); flashes = []
    for i in range(50):
        at = 10 + i * .1; before = effect.flashes
        event = dict(id=i + 1, at=at, kind='frequency_bass', strength=1.)
        effect.factors([event], at, 2, 'frequency', 'together', mode=mode)
        if before < effect.flashes: flashes.append(at)
    if mode == 'beat_flash': assert all(delta >= 1/3 for delta in np.diff(flashes))
    else: assert len(flashes) == 50


@pytest.mark.parametrize('mode', ['beat_flash', 'beat_gentle'])
def test_engine_runs_threshold_pulses_before_bpm_without_fast_white_requests(monkeypatch, mode):
    from app.animation import engine as module
    now = [100.]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    class Audio:
        running = True; phase_at = None; hit_counter = 0; events = []
        def current_bpm(self): return None
        def music_energy(self): return .2
        def recent_hits(self, after): return [hit for hit in self.events if hit['id'] > after]
    async def scenario():
        engine = Engine(MockClient()); engine.audio = Audio()
        engine.settings = Settings(mode=mode, flash_trigger='frequency', flash_pattern='staggered', bpm_source='audio', pulse_palette='fixed')
        try:
            await engine.start()
            assert [light.brightness for light in engine.client.lights[:2]] == [12, 12]
            count = len(engine.client.commands)
            engine.audio.events = [dict(id=1, at=100., kind='frequency_bass', strength=1.)]
            await engine.apply()
            assert [light.brightness for light in engine.client.lights[:2]] == [60, 12]
            now[0] = 100.5
            engine.audio.events.append(dict(id=2, at=100.5, kind='frequency_mids', strength=1.))
            await engine.apply()
            assert [light.brightness for light in engine.client.lights[:2]] == [12, 60]
            assert len(engine.client.commands) == count
            assert engine.stream is not None
        finally:
            await engine.restore()
        assert engine.stream is None and not any(light.on for light in engine.client.lights)
    asyncio.run(scenario())


def test_settings_validate_threshold_order_and_finite_ranges():
    for values in [dict(bass_fallback_db=-10), dict(mids_threshold_db=1), dict(bass_threshold_db=-91), dict(mids_threshold_db=float('nan'))]:
        with pytest.raises(ValidationError): Settings(**values)
    assert Settings(flash_trigger='frequency').bass_threshold_db == -15


def test_api_threshold_edits_reconfigure_capture_and_clear_old_crossings(monkeypatch, tmp_path):
    from app import main, config
    samples = .35 * np.sin(2 * np.pi * 1000 * np.arange(12000) / 12000)
    fake_input(monkeypatch, np.column_stack((samples, -samples)).astype('float32'))
    audio = LiveBPM()
    monkeypatch.setattr(main, 'audio', audio)
    monkeypatch.setattr(config, 'MOCK_MODE', True)
    monkeypatch.setattr(config, 'MUSIC_ENABLED', False)
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
    with TestClient(main.app) as client:
        values = dict(mode='beat_gentle', flash_trigger='frequency', bass_threshold_db=-25, bass_fallback_db=-60, mids_threshold_db=-22)
        assert client.put('/api/settings', json=values).status_code == 200
        assert not audio.running
        assert client.post('/api/audio/start', json={'device_id':1}).status_code == 200
        assert audio.frequency.gate.bass == -25 and audio.frequency.gate.mids == -22
        assert any(hit['kind'] == 'frequency_mids' for hit in audio.hits)
        previous = audio.frequency
        values['bass_threshold_db'] = -10
        assert client.put('/api/settings', json=values).status_code == 200
        assert audio.frequency is not previous
        assert not any(hit['kind'].startswith('frequency_') for hit in audio.hits)
        assert main.engine.hit_flashes.cursor == audio.hit_counter
        values['bass_fallback_db'] = -5
        assert client.put('/api/settings', json=values).status_code == 422
        assert audio.frequency.gate.fallback == -60
        page = client.get('/').text
        assert 'value="frequency"' in page and 'data-setting="bass_threshold_db"' in page
        assert client.post('/api/audio/stop').status_code == 200
        assert audio.frequency is None


def test_track_changes_reset_gate_history_and_stale_capture_hides_levels(monkeypatch):
    fake_input(monkeypatch, np.zeros((12000, 2), dtype='float32'))
    async def scenario():
        audio = LiveBPM()
        try:
            await audio.start(1)
            original = audio.frequency
            original.gate.update(-10, -90, time.monotonic())
            audio.track_changed()
            assert audio.frequency is not original and audio.frequency.gate.armed['bass']
            assert audio.spectrum()['threshold_gate'] is None
            assert audio.status()['frequency'] is None
        finally: await audio.stop()
    asyncio.run(scenario())


def test_silence_rearms_even_at_lowest_configurable_mid_threshold():
    detector = FrequencyDetector(48000, mids=-90)
    n = 48000
    tone = .001 * np.sin(2 * np.pi * 1000 * np.arange(n) / 48000)
    first = detector.process(tone, 1)
    detector.process(np.zeros(n), 2)
    second = detector.process(tone, 3)
    assert [hit['kind'] for hit in first] == ['frequency_mids']
    assert [hit['kind'] for hit in second] == ['frequency_mids']
