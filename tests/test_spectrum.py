import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.audio.live import LiveBPM
from app.audio.spectrum import WINDOW_SIZE, analyze_spectrum
from tests.test_audio_reliability import fake_input


def tone(frequency, amplitude=.5, sample_rate=48000):
    return amplitude * np.sin(2 * np.pi * frequency * np.arange(WINDOW_SIZE) / sample_rate)


@pytest.mark.parametrize('frequency,band', [(94, 'bass'), (1002, 'mids'), (7998, 'treble')])
def test_known_tones_have_correct_peak_band_and_calibrated_level(frequency, band):
    result = analyze_spectrum(tone(frequency), 48000)
    assert result['peak_frequency'] == pytest.approx(frequency, abs=48000 / WINDOW_SIZE)
    assert result['peak_db'] == pytest.approx(-6.02, abs=1.5)
    dominant = max(result['bands'], key=lambda item: item['rms'])
    assert dominant['id'] == band
    assert dominant['rms'] == pytest.approx(.5 / np.sqrt(2), rel=.01)
    assert dominant['db'] == pytest.approx(-9.03, abs=.1)
    assert not result['silent']
    assert len(result['levels_db']) == len(result['frequencies']) == 72
    assert len(result['edges']) == 73


def test_volume_changes_are_visible_instead_of_auto_normalized():
    loud = analyze_spectrum(tone(1002, .5), 48000)
    quiet = analyze_spectrum(tone(1002, .05), 48000)
    assert loud['peak_db'] - quiet['peak_db'] == pytest.approx(20, abs=.15)
    assert loud['bands'][1]['db'] - quiet['bands'][1]['db'] == pytest.approx(20, abs=.15)


def test_silence_and_dc_have_no_false_peak_and_json_has_finite_values():
    for samples in [np.zeros(WINDOW_SIZE), np.ones(WINDOW_SIZE) * .5]:
        result = analyze_spectrum(samples, 48000)
        assert result['silent'] and result['peak_frequency'] is None
        assert result['levels_db'] == [-90] * 72
        assert all(band['rms'] == 0 for band in result['bands'])
        json.dumps(result, allow_nan=False)


def test_spectrum_obeys_nyquist_and_rejects_invalid_input():
    result = analyze_spectrum(tone(4000, sample_rate=12000), 12000)
    assert result['max_frequency'] == 6000
    assert result['bands'][-1]['high'] == result['edges'][-1] == 6000
    assert result['peak_frequency'] == pytest.approx(4000, abs=2)
    with pytest.raises(ValueError): analyze_spectrum(np.full(WINDOW_SIZE, np.nan), 48000)
    with pytest.raises(ValueError): analyze_spectrum(np.zeros(100), 48000)


def test_live_spectrum_uses_only_recent_window_and_clears_stale_data():
    audio = LiveBPM()
    assert audio.spectrum()['state'] == 'idle'
    audio.running = True
    assert audio.spectrum()['state'] == 'waiting_input'
    audio.sample_rate = 48000
    audio.buffer.extend([tone(100), tone(4000)[:2048], tone(4000)[2048:]])
    audio.frames = WINDOW_SIZE * 2
    audio.end_time = time.monotonic()
    result = audio.spectrum()
    assert result['ready'] and result['peak_frequency'] == pytest.approx(4000, abs=6)
    audio.end_time -= 1
    assert audio.spectrum()['state'] == 'stalled'
    assert 'levels_db' not in audio.spectrum()
    audio.track_changed()
    assert audio.spectrum()['state'] == 'waiting_input'
    assert not audio.spectrum()['ready']


def test_restart_during_fft_does_not_publish_previous_track(monkeypatch):
    from app.audio import live
    audio = LiveBPM()
    audio.running = True
    audio.buffer.append(tone(100))
    audio.frames = WINDOW_SIZE
    audio.end_time = time.monotonic()
    analyze = live.analyze_spectrum
    def changing_track(samples, rate):
        result = analyze(samples, rate)
        audio.track_changed()
        return result
    monkeypatch.setattr(live, 'analyze_spectrum', changing_track)
    result = audio.spectrum()
    assert not result['ready'] and 'levels_db' not in result


def test_capture_opposite_polarity_stereo_feeds_spectrum_and_stop_clears_it(monkeypatch):
    samples = tone(1002, sample_rate=12000).astype('float32')
    fake_input(monkeypatch, np.column_stack((samples, -samples)))
    async def scenario():
        audio = LiveBPM()
        try:
            await audio.start(1)
            result = audio.spectrum()
            assert result['ready'] and result['peak_frequency'] == pytest.approx(1002, abs=2)
            assert result['bands'][1]['rms'] > .3
        finally:
            await audio.stop()
        assert audio.spectrum()['state'] == 'idle'
        assert 'levels_db' not in audio.spectrum()
    asyncio.run(scenario())


def test_spectrum_api_is_read_only_opt_in_and_returns_only_derived_audio(monkeypatch, tmp_path):
    from app import main, config
    audio = LiveBPM()
    monkeypatch.setattr(main, 'audio', audio)
    monkeypatch.setattr(config, 'MOCK_MODE', True)
    monkeypatch.setattr(config, 'MUSIC_ENABLED', False)
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
    with TestClient(main.app) as client:
        response = client.get('/api/audio/spectrum')
        assert response.status_code == 200 and response.json()['state'] == 'idle'
        assert not audio.running and audio.stream is None
        audio.running = True
        audio.buffer.append(tone(1002))
        audio.frames = WINDOW_SIZE
        audio.end_time = time.monotonic()
        result = client.get('/api/audio/spectrum').json()
        assert result['ready'] and len(result['levels_db']) == 72
        assert 'samples' not in result and 'buffer' not in result
        assert main.engine.status == 'stopped'
        page = client.get('/').text
        assert 'spectrumCanvas' in page and '/static/spectrum.js' in page
        assert client.get('/static/spectrum.js').status_code == 200


def test_browser_color_blend_and_live_graph():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is only used for optional browser-script verification')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(root / 'tests/spectrum_browser.cjs')], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
