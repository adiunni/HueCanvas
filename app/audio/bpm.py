"""Tempo and beat-phase estimation from PCM; no recording is saved.

DJ-style analysis: a multiband onset envelope is folded at each candidate beat
period ("pulse train") to measure how strongly beats line up at that tempo.
Related tempos (double, half, 3:2 …) are compared with a soft preference for
typical dance tempos around 120 BPM. Below 80 BPM, regular activity between the
beats (hats, ghost notes) means a half-time feel over a double-time beat, so the
tempo is doubled; a bare pulse is left alone. Drum and bass at 174 BPM usually
reads as 87, as in DJ software's default tempo ranges.
"""
import math
from collections import deque
import numpy as np
from scipy.signal import correlate, resample_poly, find_peaks
from scipy.ndimage import gaussian_filter1d

RATE = 100; HOP = 240; SIZE = 1024; FS = 24000
# The top band reaches 12 kHz so hi-hats count towards the beat.
BANDS = ((30, 200), (200, 2000), (2000, 12000))
# Settable tempo range. 512 covers a 1/16-note loop roll on a 128 BPM track.
MIN_TEMPO, MAX_TEMPO = 30, 512
# Detection searches up to the ceiling, but the 120 BPM preference keeps the
# reading on the track's tempo during rolls and dense hi-hats, as DJ software does.
LOWEST, HIGHEST = 55, MAX_TEMPO


def align_beat_phase(previous_at, latest_at, bpm):
    """Correct phase without resetting multi-beat patterns every analysis window."""
    if previous_at is None: return latest_at
    period=60/bpm
    return latest_at-round((latest_at-previous_at)/period)*period


def onset_envelope(signal):
    """Sum of per-band spectral flux at 100 frames/s; frame i ends at (i+1)*HOP+SIZE."""
    windows = np.lib.stride_tricks.sliding_window_view(signal, SIZE)[::HOP]
    spectrum = np.abs(np.fft.rfft(windows * np.hanning(SIZE), axis=1))
    frequencies = np.fft.rfftfreq(SIZE, 1 / FS)
    envelope = np.zeros(len(spectrum) - 1)
    for low, high in BANDS:
        band = np.log1p(100 * spectrum[:, (frequencies >= low) & (frequencies < high)])
        flux = np.maximum(np.diff(band, axis=0), 0).mean(axis=1)
        flux = np.maximum(flux - gaussian_filter1d(flux, 25), 0)
        envelope += flux / max(float(np.percentile(flux, 95)), 1e-9)
    return envelope


def folded(envelope, bpm):
    """Mean envelope per beat phase, above the envelope median."""
    period = 60 * RATE / bpm
    bins = max(8, int(round(period)))
    index = np.floor((np.arange(len(envelope)) % period) / period * bins).astype(int)
    sums = np.bincount(index, envelope, bins); counts = np.maximum(np.bincount(index, minlength=bins), 1)
    return gaussian_filter1d(sums / counts, 1, mode='wrap') - np.median(envelope)


def strength(envelope, bpm):
    return float(folded(envelope, bpm).max())


def prior(bpm):
    return math.exp(-.5 * (math.log2(bpm / 120) / .9) ** 2)


def choose_tempo(envelope, previous_bpm=None):
    tempos = np.concatenate((np.arange(LOWEST, 210, .25), np.arange(210, HIGHEST + .5, .5)))
    scores = np.array([strength(envelope, tempo) for tempo in tempos])
    peaks, _ = find_peaks(scores)
    if not len(peaks): return None
    candidates = set()
    for tempo in tempos[peaks[np.argsort(scores[peaks])[::-1][:6]]]:
        for ratio in (1, 2, .5, 1.5, 2 / 3, 4 / 3, .75):
            if LOWEST <= tempo * ratio <= HIGHEST: candidates.add(round(tempo * ratio * 4) / 4)
    best = None
    for tempo in candidates:
        fine = np.arange(tempo - .5, tempo + .5, .05)
        values = [strength(envelope, value) for value in fine]
        value = float(fine[int(np.argmax(values))]); score = max(values) * prior(value)
        # Continuity with the last estimate avoids flips between near-equal readings.
        if previous_bpm and abs(value - previous_bpm) < 1.5: score *= 1.05
        if best is None or score > best[0]: best = (score, value)
    bpm = best[1]
    if bpm < 110 and bpm * 2 <= HIGHEST:
        means = folded(envelope, bpm); beat = int(np.argmax(means))
        between = means[(beat + len(means) // 2) % len(means)] / max(means[beat], 1e-12)
        if between >= (.03 if bpm < 80 else .75): bpm *= 2
    return bpm


def detect_bpm(samples, sample_rate, previous_bpm=None):
    signal = np.asarray(samples, dtype=np.float64)
    if signal.ndim == 2:
        # Opposite-polarity stereo channels must not cancel music to silence.
        signal = signal[:,np.argmax(np.mean(signal**2,axis=0))] if signal.shape[1] else np.array([])
    if sample_rate < 8000 or signal.size < sample_rate * 6 or not np.isfinite(signal).all():
        return None
    rms = float(np.sqrt(np.mean(signal[-sample_rate:] ** 2)))
    if rms < .0005:
        return None
    divisor = math.gcd(int(sample_rate), FS)
    signal = resample_poly(signal, FS // divisor, int(sample_rate) // divisor)
    envelope = onset_envelope(signal)
    if envelope.std() < 1e-5:
        return None
    # Periodicity confidence: autocorrelation of the smoothed envelope at the beat.
    smooth = gaussian_filter1d(envelope, 1.5); centered = smooth - smooth.mean()
    ac = correlate(centered, centered, mode='full', method='fft')[len(centered)-1:]
    bpm = choose_tempo(envelope, previous_bpm)
    if bpm is None: return None
    # A half-time groove repeats most strongly every two beats; confidence
    # takes the stronger of the beat and two-beat periods.
    lags = [int(60 * RATE / bpm * k) for k in (1, 2) if int(60 * RATE / bpm * k) + 2 <= len(ac)]
    confidence = float(np.clip(max(ac[lag:lag + 2].max() for lag in lags) / max(ac[0], 1e-12), 0, 1)) if lags else 0.
    if confidence < .12:
        return None
    period = 60 / bpm
    means = folded(envelope, bpm)
    phase_seconds = (int(np.argmax(means)) + .5) / len(means) * period
    # Envelope frame i compares windows ending at (i+1)·HOP+SIZE and earlier;
    # an attack registers about half a window after it starts.
    origin = (HOP + SIZE / 2) / FS + phase_seconds
    end = (len(envelope) - 1) / RATE + (HOP + SIZE / 2) / FS
    last_beat_seconds = origin + math.floor((end - origin) / period) * period
    return {'bpm': round(bpm, 2), 'confidence': round(confidence, 3),
            'last_beat_seconds': last_beat_seconds, 'duration': len(signal)/FS, 'rms': rms}


class TempoTracker:
    """Turn per-window estimates into a steady DJ-style tempo.

    The displayed tempo is the median of recent estimates. A single window at
    a related tempo (double, half) is ignored; a different tempo must persist
    for three windows. Once stable, the tempo snaps to a whole BPM when it is
    within 0.15 of one, as most produced tracks are.
    """
    def __init__(self):
        self.reset()

    def reset(self):
        self.history = deque(maxlen=5); self.bpm = None; self.locked = False; self.pending = deque(maxlen=3)

    def update(self, estimate):
        if estimate is None: return self.bpm
        if self.bpm and abs(estimate - self.bpm) > self.bpm * .04:
            self.pending.append(estimate)
            agree = len(self.pending) == 3 and max(self.pending) - min(self.pending) <= max(self.pending) * .04
            if not agree: return self.bpm
            self.history.clear(); self.history.extend(self.pending); self.locked = False
        self.pending.clear(); self.history.append(estimate)
        median = float(np.median(self.history))
        spread = max(self.history) - min(self.history)
        self.locked = len(self.history) >= 3 and spread <= max(.6, median * .006)
        whole = round(median)
        self.bpm = float(whole) if self.locked and abs(median - whole) <= .15 else round(median, 2)
        return self.bpm
