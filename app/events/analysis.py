"""Conservative section candidates from authorized PCM WAVs, using NumPy/SciPy.

Scores are heuristic evidence strengths, not calibrated probabilities. Beat and
downbeat arrays are candidates (4/4 phase hypothesis), not meter recognition.
"""
import hashlib
import io
import math
import warnings
import numpy as np
from scipy.io import wavfile
from scipy.signal import find_peaks, resample_poly
from scipy.ndimage import uniform_filter1d
from app.events.models import ALGORITHM_VERSION, Cue, DEFAULT_EFFECT, EventType

FS = 12000
HOP = 240
SIZE = 1024
STEP = HOP / FS
MAX_BYTES = 128 * 1024 * 1024
MAX_SECONDS = 1200


def robust(values):
    values = np.asarray(values, dtype=float)
    median = np.median(values)
    scale = max(1e-8, 1.4826 * float(np.median(abs(values - median))),
                float(np.percentile(values, 90) - np.percentile(values, 10)) / 2)
    return np.clip((values - median) / scale, -8, 8)


def read_wav(data):
    if len(data) > MAX_BYTES:
        raise ValueError('WAV must be at most 128 MB')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', wavfile.WavFileWarning)
            rate, samples = wavfile.read(io.BytesIO(data))
    except (ValueError, EOFError, OSError) as exc:
        raise ValueError('Choose an uncompressed PCM or float WAV file') from exc
    if not 8000 <= rate <= 192000 or samples.ndim not in (1, 2) or (samples.ndim == 2 and samples.shape[1] > 2):
        raise ValueError('WAV must be mono/stereo at 8–192 kHz')
    if not 2 <= len(samples) / rate <= MAX_SECONDS:
        raise ValueError('WAV duration must be 2 seconds to 20 minutes')
    if samples.dtype.kind == 'u':
        samples = (samples.astype(np.float32) - 128) / 128
    elif samples.dtype.kind == 'i':
        samples = samples.astype(np.float32) / (2 ** (samples.dtype.itemsize * 8 - 1))
    else:
        samples = samples.astype(np.float32)
    if not np.isfinite(samples).all() or np.max(abs(samples)) > 8:
        raise ValueError('WAV contains invalid or out-of-range samples')
    # Avoid cancellation on opposite-polarity stereo, matching live input policy.
    if samples.ndim == 2:
        samples = samples[:, int(np.argmax(np.mean(samples ** 2, axis=0)))]
    return samples, rate


def features(samples, rate):
    signal = np.asarray(samples, dtype=np.float32)
    if signal.ndim != 1 or not np.isfinite(signal).all() or rate < 1000:
        raise ValueError('Feature input must be finite mono audio')
    duration = len(signal) / rate
    if rate != FS:
        divisor = math.gcd(int(rate), FS)
        signal = resample_poly(signal, FS // divisor, int(rate) // divisor)
    if len(signal) < SIZE:
        raise ValueError('Audio is too short to analyze')
    windows = np.lib.stride_tricks.sliding_window_view(signal, SIZE)[::HOP]
    window = np.hanning(SIZE)
    frequencies = np.fft.rfftfreq(SIZE, 1 / FS)
    masks = [(frequencies >= a) & (frequencies < b) for a, b in
             [(25, 200), (200, 600), (600, 1800), (1800, 3500), (3500, 6001)]]
    rms, bands, flux = [], [], []
    previous = np.zeros(SIZE // 2 + 1)
    # Bounded FFT batches keep long files off the event loop and avoid a huge STFT.
    for start in range(0, len(windows), 1024):
        block = windows[start:start + 1024]
        rms.extend(np.sqrt(np.mean(block ** 2, axis=1)))
        spectrum = abs(np.fft.rfft(block * window, axis=1)) / window.sum()
        bands.extend(np.array([np.sqrt(np.sum(spectrum[:, mask] ** 2, axis=1)) for mask in masks]).T)
        compressed = np.log1p(100 * spectrum)
        flux.extend(np.maximum(np.diff(np.vstack([previous, compressed]), axis=0), 0).mean(axis=1))
        previous = compressed[-1]
    rms = np.asarray(rms)
    bands = np.asarray(bands)
    flux = np.asarray(flux)
    times = np.arange(len(rms)) * STEP + SIZE / (2 * FS)
    onset = np.maximum(0, flux - uniform_filter1d(flux, size=51))
    threshold = max(.0001, float(np.median(onset) + 1.5 * np.median(abs(onset - np.median(onset)))), float(np.percentile(onset, 90)) * .2)
    peaks, _ = find_peaks(onset, height=threshold, distance=8)
    # Flux alone counts tiny spectral fluctuations in noisy reverb as attacks.
    # Require a local energy rise before adding an onset to activity density.
    peaks = np.asarray([i for i in peaks if i >= 5 and rms[i] > max(.0001, float(np.median(rms[i-5:i])) * 1.12)], dtype=int)
    impulses = np.zeros(len(rms)); impulses[peaks] = 1
    density = uniform_filter1d(impulses, size=101) / STEP
    logbands = np.log(np.maximum(bands, 1e-6))
    novelty = np.zeros(len(rms))
    energy_change = np.zeros(len(rms))
    boundary = 75
    for i in range(boundary, len(rms) - boundary):
        novelty[i] = np.linalg.norm(np.mean(logbands[i:i + boundary], axis=0) - np.mean(logbands[i - boundary:i], axis=0))
        energy_change[i] = math.log((np.mean(rms[i:i + boundary]) + 1e-5) / (np.mean(rms[i - boundary:i]) + 1e-5))
    beats, downbeats, bpm = beat_candidates(times[peaks], onset[peaks], duration)
    silent = rms < max(.00005, float(np.max(rms)) * .002)
    active = np.flatnonzero(~silent)
    end = min(duration, float(times[active[-1]] + SIZE / (2 * FS))) if len(active) else 0.
    values = dict(time=times, rms=rms, bass=bands[:, 0], flux=flux, onset=onset,
                  density=density, novelty=novelty, energy_change=energy_change,
                  tail_energy=uniform_filter1d(rms ** 2, size=101), silence=silent.astype(float))
    return {'duration': duration, 'audible_end': end, 'step': STEP, 'bpm': bpm,
            'beat_candidates': beats, 'downbeat_candidates': downbeats,
            'onset_indices': peaks.tolist(),
            'features': {k: np.round(v, 7).tolist() for k, v in values.items()},
            'normalized': {k: np.round(robust(v), 5).tolist() for k, v in values.items() if k not in ('time', 'silence')}}


def beat_candidates(onsets, strengths, duration):
    if len(onsets) < 8:
        return [], [], None
    gaps = np.diff(onsets)
    valid = gaps[(gaps >= .25) & (gaps <= 1)]
    if len(valid) < 4:
        return [], [], None
    period = float(np.median(valid))
    agreement = float(np.mean(abs(valid / period - np.round(valid / period)) < .15))
    if agreement < .65:
        return [], [], None
    anchor = float(onsets[np.argmax(strengths)])
    anchor %= period
    beats = np.arange(anchor, duration, period)
    # Choose the strongest of four beat phases; meter itself is not inferred.
    scores = [sum(float(s) for t, s in zip(onsets, strengths) if round((t - anchor) / period) % 4 == phase) for phase in range(4)]
    return beats.round(4).tolist(), beats[int(np.argmax(scores))::4].round(4).tolist(), round(60 / period, 2)


def candidate(track_id, kind, position, confidence, measurements):
    return Cue(id=hashlib.sha256(f'{track_id}:{kind}:{position:.1f}'.encode()).hexdigest()[:24],
               track_id=track_id, type=kind, position=round(max(0, position), 3), source='offline',
               confidence=round(min(.98, max(0, confidence)), 3), algorithm_version=ALGORITHM_VERSION,
               features={k: float(v) for k, v in measurements.items()}, effect=DEFAULT_EFFECT[EventType(kind)],
               review='pending')


def detect(track_id, analysis):
    f = {k: np.asarray(v) for k, v in analysis['features'].items()}
    rms, bass, times = f['rms'], f['bass'], f['time']
    peaks = analysis['onset_indices']
    end = analysis['audible_end']
    if np.max(rms) < .0001:
        return []
    drops = []
    boundaries = []
    for i in peaks:
        at = times[i]
        if at < 4 or at + 2 > end:
            continue
        before = slice(max(0, i - 150), max(1, i - 5))
        after = slice(i + 5, min(len(rms), i + 100))
        lift = float(np.mean(rms[after]) / max(1e-5, np.mean(rms[before])))
        bass_return = float(np.mean(bass[after]) / max(1e-5, np.mean(bass[before])))
        activity = float(np.mean(f['density'][after]) / max(.25, np.mean(f['density'][before])))
        novelty = float(f['novelty'][i])
        aligned = min((abs(at - beat) for beat in analysis['beat_candidates']), default=1) < .12
        repeated = sum(i <= j <= i + 100 for j in peaks) >= 3
        if repeated and lift > 1.7 and (bass_return > 1.5 or activity > 1.7) and novelty > .5 and np.mean(f['density'][after]) >= 1:
            confidence = min(.95, .55 + .12 * min(2, math.log2(lift)) + .08 * min(1, novelty / 2) + .04 * aligned)
            drops.append(candidate(track_id, 'DROP', at, confidence,
                                   dict(energy_ratio=lift, bass_return=bass_return, rhythm_ratio=activity,
                                        novelty=novelty, beat_aligned=aligned)))
        if lift < .55 and novelty > .8:
            boundaries.append(candidate(track_id, 'BREAKDOWN', at, .72, dict(energy_ratio=lift, novelty=novelty)))
    # Score-first section suppression, then chronological output; kicks within a
    # lifted section cannot each become a drop.
    selected = []
    for cue in sorted(drops, key=lambda c: c.confidence, reverse=True):
        if all(abs(cue.position - other.position) >= 8 for other in selected):
            selected.append(cue)
    for drop in list(selected):
        i = int(np.searchsorted(times, drop.position))
        early = rms[max(0, i - 350):max(1, i - 200)]
        late = rms[max(0, i - 150):max(1, i - 15)]
        if len(early) and len(late) and np.mean(late) > max(.0002, np.mean(early) * 1.25):
            selected.extend([candidate(track_id, 'BUILD_START', max(0, drop.position - 6), .65, {'rise': float(np.mean(late) / max(1e-6, np.mean(early)))}),
                             candidate(track_id, 'BUILD_END', drop.position, drop.confidence, {})])
    # Gradual multi-second rises also count without a subsequent drop. Require
    # three successive section averages to rise, excluding a single loud hit.
    for i in range(300, len(rms), 50):
        sections = [float(np.mean(rms[i-300+j*75:i-225+j*75])) for j in range(4)]
        if (sections[0] > .0001 and sections[-1] / sections[0] > 1.65 and
                all(b > a * 1.08 for a, b in zip(sections, sections[1:]))):
            start = float(times[i-300])
            if all(abs(start-c.position) >= 8 for c in selected if c.type == EventType.BUILD_START):
                selected.append(candidate(track_id, 'BUILD_START', start, .72, {'rise': sections[-1]/sections[0]}))
                finish = next((c.position for c in sorted(selected,key=lambda c:c.position) if c.type == EventType.DROP and c.position > start), None)
                if finish is not None and not any(c.type == EventType.BUILD_END and abs(c.position-finish)<1 for c in selected):
                    selected.append(candidate(track_id,'BUILD_END',finish,.72,{}))
    for cue in boundaries:
        if all(abs(cue.position - c.position) >= 8 for c in selected):
            selected.append(cue)
    impact = final_impact(track_id, analysis)
    if impact:
        selected = [c for c in selected if not (c.type in (EventType.DROP, EventType.BUILD_END) and abs(c.position-impact.position)<2)]
        selected.append(impact)
    else:
        # Sustained tail reduction can suggest a fade or DJ outro, never a hit.
        section = max(4, min(16, end * .25))
        earlier = rms[(times >= end - section * 2) & (times < end - section)]
        tail = rms[(times >= end - section) & (times < end)]
        if len(earlier) and len(tail) and np.mean(tail) < np.mean(earlier) * .6:
            selected.append(candidate(track_id, 'OUTRO_START', end - section, .66,
                                      {'tail_ratio': float(np.mean(tail) / max(1e-6, np.mean(earlier)))}))
    selected.append(candidate(track_id, 'TRACK_END', end, .9, {'trailing_silence': analysis['duration'] - end}))
    return sorted(selected, key=lambda c: c.position)


def final_impact(track_id, analysis):
    f = {k: np.asarray(v) for k, v in analysis['features'].items()}
    rms, times = f['rms'], f['time']
    end = analysis['audible_end']
    # Search the final musical region, including arbitrarily long decays. The
    # winning onset is measured from evidence, never file-end minus a constant.
    global_reference = max(.0001, float(np.percentile(rms[times < end], 85)))
    accents, _ = find_peaks(rms, distance=12, prominence=global_reference * .25)
    ranked = []
    for i in accents:
        at = times[i]
        if at + .8 >= analysis['duration']:
            continue
        preceding = rms[(times >= at - 6) & (times < at - .15)]
        following = rms[times >= at + .6]
        if len(preceding) < 25 or len(following) < 25:
            continue
        reference = max(global_reference, float(np.percentile(preceding, 75)))
        accent = float(rms[i] / reference)
        tail = float(np.sqrt(np.mean(following ** 2)) / max(rms[i], 1e-6))
        # Another large accent after this one means it is not the final hit.
        later = [j for j in accents if times[j] > at + .35 and rms[j] >= rms[i] * .65]
        tail_density = float(np.mean(f['density'][times >= at + 1])) if np.any(times >= at + 1) else 0
        prior_density = float(np.mean(f['density'][(times >= at - 6) & (times < at - .15)]))
        tail_active = following[:max(1, min(len(following), 150))]
        decay = float(np.mean(tail_active[:max(1, len(tail_active)//3)]) - np.mean(tail_active[-max(1, len(tail_active)//3):]))
        if accent < 1.8 or tail > .32 or later or tail_density > max(.8, prior_density * .55) or decay < -1e-5:
            continue
        score = min(.96, .55 + .12 * min(2, math.log2(accent)) + .15 * (1 - min(1, tail / .32)))
        # Recover the attack just before the peak, rather than scheduling the
        # maximum of the analysis window half a beat late.
        nearby = [j for j in analysis['onset_indices'] if 0 <= at - times[j] <= .2]
        onset = float(times[nearby[-1]]) if nearby else float(at)
        ranked.append(candidate(track_id, 'FINAL_IMPACT', onset, score,
                                dict(accent_ratio=accent, tail_ratio=tail, tail_onset_density=tail_density, decay=decay)))
    return max(ranked, key=lambda c: (c.confidence, c.position)) if ranked else None


def analyze_wav(data, track_id):
    samples, rate = read_wav(data)
    result = features(samples, rate)
    result.update(algorithm_version=ALGORITHM_VERSION, audio_sha256=hashlib.sha256(data).hexdigest())
    return detect(track_id, result), result
