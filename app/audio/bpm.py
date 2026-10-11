"""Tempo and beat-phase estimation from PCM; no recording is saved."""
import math
import numpy as np
from scipy.signal import correlate, resample_poly, find_peaks
from scipy.ndimage import gaussian_filter1d


def align_beat_phase(previous_at, latest_at, bpm):
    """Correct phase without resetting multi-beat patterns every analysis window."""
    if previous_at is None: return latest_at
    period=60/bpm
    return latest_at-round((latest_at-previous_at)/period)*period


def detect_bpm(samples, sample_rate, previous_bpm=None):
    signal = np.asarray(samples, dtype=np.float64)
    if signal.ndim == 2:
        # Opposite-polarity stereo channels must not cancel music to silence.
        signal = signal[:,np.argmax(np.mean(signal**2,axis=0))] if signal.shape[1] else np.array([])
    if sample_rate < 8000 or signal.size < sample_rate * 8 or not np.isfinite(signal).all():
        return None
    rms = float(np.sqrt(np.mean(signal[-sample_rate:] ** 2)))
    if rms < .0005:
        return None
    divisor = math.gcd(int(sample_rate), 12000)
    signal = resample_poly(signal, 12000 // divisor, int(sample_rate) // divisor)
    hop, size, rate = 240, 1024, 50
    windows = np.lib.stride_tricks.sliding_window_view(signal, size)[::hop]
    spectrum = np.abs(np.fft.rfft(windows * np.hanning(size), axis=1))
    frequencies = np.fft.rfftfreq(size, 1 / 12000)
    band = (frequencies >= 35) & (frequencies <= 4000)
    compressed = np.log1p(spectrum[:, band] * 10)
    flux = np.maximum(np.diff(compressed, axis=0), 0).mean(axis=1)
    onset = np.maximum(flux - gaussian_filter1d(flux, 5), 0)
    if onset.std() < 1e-5:
        return None
    centered = onset - onset.mean()
    ac = correlate(centered, centered, mode='full', method='fft')[len(onset)-1:]
    lo, hi = math.ceil(60 * rate / 190), math.floor(60 * rate / 55)
    candidates = np.arange(lo, hi+1)
    scores = ac[candidates].copy()
    if previous_bpm:
        # A small prior reduces noisy octave flips, without locking onto old songs.
        tempos = 60 * rate / candidates
        scores *= 1 + .04 * np.exp(-((tempos - previous_bpm) / 5) ** 2)
    lag = int(candidates[np.argmax(scores)])
    # Regular onset intervals help resolve autocorrelation's half/third-tempo
    # aliases when the true beat period falls between frame boundaries.
    peaks, _ = find_peaks(onset, distance=int(rate*60/200), prominence=max(onset.std()*.5, onset.max()*.08))
    intervals = np.diff(peaks)/rate
    guide = None
    if len(intervals)>=7 and intervals.std()/intervals.mean()<.1:
        candidate = 60/float(intervals.mean())
        guided_lag = round(rate*60/candidate)
        if 55<=candidate<=190 and lo<=guided_lag<=hi and ac[guided_lag]>=.45*ac[lag]:
            guide=candidate;lag=guided_lag
    confidence = float(np.clip(ac[lag] / max(ac[0], 1e-12), 0, 1))
    if confidence < .15:
        return None
    refined = float(lag)
    if lo < lag < hi:
        a, b, c = ac[lag-1:lag+2]
        denominator = a - 2*b + c
        if abs(denominator) > 1e-12:
            refined += float(np.clip(.5 * (a-c) / denominator, -.5, .5))
    bpm = guide or 60 * rate / refined
    period=60/bpm
    bins=np.floor(((np.arange(len(onset))/rate)%period)/period*128).astype(int)
    phase_scores=gaussian_filter1d(np.bincount(bins,weights=onset,minlength=128),2,mode='wrap')
    phase_seconds=(int(np.argmax(phase_scores))+.5)/128*period
    origin=(size/2+hop)/12000+phase_seconds
    last_beat_seconds=origin+math.floor(((len(onset)-1)/rate+(size/2+hop)/12000-origin)/period)*period
    return {'bpm': round(bpm, 2), 'confidence': round(confidence, 3),
            'last_beat_seconds': last_beat_seconds, 'duration': len(signal)/12000, 'rms': rms}
