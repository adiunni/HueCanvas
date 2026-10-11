"""Bass-priority dBFS crossings using the same RMS bands as the spectrum."""
import numpy as np
from app.audio.spectrum import WINDOW_SIZE, FLOOR_DB, band_levels, decibels


class FrequencyThresholds:
    """Gate policy independent of audio chunking. Rearm 2 dB below threshold."""
    def __init__(self, bass=-15, fallback=-70, mids=-20):
        if not -90 <= fallback < bass <= 0 or not -90 <= mids <= 0:
            raise ValueError("Bass fallback must be below its trigger threshold; levels must be −90 to 0 dBFS")
        self.bass = bass; self.fallback = fallback; self.mids = mids
        self.armed = {'bass': True, 'mids': True}
        self.active_band = None
        self.levels = {'bass': -90.0, 'mids': -90.0, 'treble': -90.0}

    def update(self, bass_db, mids_db, at):
        self.levels.update(bass=float(np.clip(bass_db, FLOOR_DB, 0)), mids=float(np.clip(mids_db, FLOOR_DB, 0)))
        self.active_band = 'mids' if bass_db < self.fallback else 'bass'
        if bass_db <= self.bass - 2: self.armed['bass'] = True
        if mids_db <= self.mids - 2: self.armed['mids'] = True
        band = self.active_band
        level = bass_db if band == 'bass' else mids_db
        threshold = self.bass if band == 'bass' else self.mids
        if level > threshold and self.armed[band]:
            self.armed[band] = False
            return [{'kind': 'frequency_' + band, 'at': at, 'strength': 1.0, 'db': float(level)}]
        return []

    def status(self):
        return dict(levels_db=dict(self.levels), active_band=self.active_band,
                    bass_threshold_db=self.bass, bass_fallback_db=self.fallback,
                    mids_threshold_db=self.mids)


class FrequencyDetector:
    def __init__(self, sample_rate, bass=-15, fallback=-70, mids=-20):
        self.sample_rate = sample_rate
        self.hop = max(1, round(sample_rate * .01))
        self.window = np.hanning(WINDOW_SIZE)
        self.frequencies = np.fft.rfftfreq(WINDOW_SIZE, 1 / sample_rate)
        self.pending = np.empty(0)
        self.history = np.zeros(WINDOW_SIZE)
        self.received = 0
        self.gate = FrequencyThresholds(bass, fallback, mids)

    def process(self, samples, end_time):
        samples = np.asarray(samples, dtype=float).reshape(-1)
        if not len(samples) or not np.isfinite(samples).all(): return []
        samples = np.concatenate((self.pending, samples))
        length = len(samples) // self.hop * self.hop
        self.pending = samples[length:].copy()
        start = end_time - len(samples) / self.sample_rate
        hits = []
        for offset in range(0, length, self.hop):
            block = samples[offset:offset + self.hop]
            self.history[:-self.hop] = self.history[self.hop:]
            self.history[-self.hop:] = block
            self.received += self.hop
            if self.received < WINDOW_SIZE: continue
            centered = self.history - np.mean(self.history)
            transform = np.fft.rfft(centered * self.window)
            rms = band_levels(transform, self.frequencies, self.window)
            # Compare unrounded levels, preserving strict > / < boundaries.
            db = 20 * np.log10(np.maximum(rms, 1e-12))
            at = start + (offset + self.hop) / self.sample_rate
            hits.extend(self.gate.update(db[0], db[1], at))
            self.gate.levels['treble'] = float(decibels(rms[2]))
        return hits
