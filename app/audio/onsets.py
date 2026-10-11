"""Short-window bass/percussion attacks, independent of the tempo estimator."""
import math
import numpy as np
from scipy.signal import butter, sosfilt


class HitDetector:
    def __init__(self, sample_rate):
        self.sample_rate = sample_rate
        self.hop = max(1, round(sample_rate * .01))
        self.pending = np.empty(0)
        self.filters = {
            'bass': butter(3, [35, 180], btype='bandpass', fs=sample_rate, output='sos'),
            'drum': butter(3, [180, min(5000, sample_rate*.45)], btype='bandpass', fs=sample_rate, output='sos'),
        }
        self.states = {kind: np.zeros((len(sos), 2)) for kind, sos in self.filters.items()}
        self.baselines = {kind: 0. for kind in self.filters}
        self.previous = self.baselines.copy()
        self.last_hits = {kind: -math.inf for kind in self.filters}
        self.levels = self.baselines.copy()
        self.processed = 0

    def process(self, samples, end_time):
        samples = np.asarray(samples, dtype=np.float64).reshape(-1)
        if not len(samples) or not np.isfinite(samples).all(): return []
        samples = np.concatenate((self.pending, samples))
        length = len(samples)//self.hop*self.hop
        self.pending = samples[length:].copy()
        if not length: return []
        start = end_time-len(samples)/self.sample_rate
        bands = {}
        for kind, sos in self.filters.items():
            filtered, self.states[kind] = sosfilt(sos, samples[:length], zi=self.states[kind])
            bands[kind] = np.sqrt(np.mean(filtered.reshape(-1, self.hop)**2, axis=1))
        hits = []
        for i in range(length//self.hop):
            self.processed += self.hop
            at = start+(i+1)*self.hop/self.sample_rate
            for kind, levels in bands.items():
                level = float(levels[i]); baseline = self.baselines[kind]
                ratio = level/max(baseline, .0003)
                other = bands['drum' if kind=='bass' else 'bass'][i]
                if (self.processed >= self.sample_rate*.15 and level > max(.0008, other*.35)
                        and ratio > 1.3 and level > self.previous[kind]*1.35
                        and at-self.last_hits[kind] >= .08):
                    hits.append({'kind': kind, 'at': at, 'strength': ratio})
                    self.last_hits[kind] = at
                self.baselines[kind] = .98*baseline+.02*level
                self.previous[kind] = level
                self.levels[kind] = level
        return sorted(hits, key=lambda hit: hit['at'])
