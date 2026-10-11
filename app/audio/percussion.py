"""Causal multiband changes, repeated attacks and musical-section contrast.

This estimates percussion patterns in a mix; it does not separate instruments.
"""
import math
from collections import deque
import numpy as np


class RollTracker:
    def __init__(self):
        self.attacks = deque(maxlen=6)
        self.until = -math.inf
        self.interval = None

    def add(self, at, profile, family):
        profile = np.asarray(profile, dtype=float)
        profile = profile / max(float(np.linalg.norm(profile)), 1e-12)
        if self.attacks:
            previous = self.attacks[-1]
            gap = at - previous[0]
            similar = float(np.dot(profile, previous[1])) >= .72
            if not .045 <= gap <= .26 or not similar or family != previous[2]:
                self.attacks.clear(); self.until = -math.inf
        self.attacks.append((at, profile, family))
        if len(self.attacks) < 4: return False
        intervals = np.diff([entry[0] for entry in self.attacks])[-3:]
        changes = intervals[1:] / intervals[:-1]
        if np.all((changes >= .6) & (changes <= 1.5)):
            self.interval = float(np.median(intervals))
            self.until = at + min(.5, max(.16, self.interval * 2.5))
            return True
        self.until = -math.inf
        return False

    def status(self, now):
        active = now <= self.until
        return dict(roll_active=active, roll_rate=round(1 / self.interval, 1) if active else None,
                    roll_hits=len(self.attacks) if active else 0)


class PercussionDetector:
    BAND_NAMES = ('low', 'body', 'crack', 'air')

    def __init__(self, sample_rate):
        self.sample_rate = sample_rate; self.hop = max(1, round(sample_rate * .01))
        self.window = np.hanning(self.hop * 2)
        self.size = 2 ** math.ceil(math.log2(len(self.window)))
        frequencies = np.fft.rfftfreq(self.size, 1 / sample_rate)
        self.masks = [(frequencies >= low) & (frequencies < min(high, sample_rate * .48))
                      for low, high in ((40, 180), (180, 1000), (1000, 4000), (4000, 12000))]
        self.pending = np.empty(0); self.history = np.zeros(self.hop * 2)
        self.previous = np.zeros(len(frequencies)); self.previous_levels = np.zeros(4)
        self.flux_history = deque(maxlen=100)
        self.processed = 0; self.last_hit = -math.inf; self.level = 0
        self.changes = np.zeros(4); self.pattern = RollTracker(); self.last_family = None
        self.energy_history = deque(maxlen=600); self.fast_energy = 0; self.bar_seconds = 2.
        self.candidate = None; self.last_drop = -math.inf; self.drops = []; self.last_drop_info = None

    def status(self, now):
        return dict(self.pattern.status(now), band_change=dict(zip(self.BAND_NAMES, self.changes.tolist())),
                    last_family=self.last_family if now - self.last_hit < .75 else None,
                    comparison_seconds=round(self.bar_seconds, 2))

    def _drop(self, at, rms, levels, attack, rolling):
        # Compare against the preceding four-beat-length window when tempo is
        # known. Its position is not assumed to be a musical downbeat.
        window = [entry for entry in self.energy_history if at - self.bar_seconds <= entry[0] < at - .1]
        if len(window) >= min(150, round(self.bar_seconds * 65)):
            bed = float(np.median([entry[1] for entry in window]))
            previous_bands = np.median([entry[2] for entry in window], axis=0)
            lift = self.fast_energy / max(.0005, bed)
            band_lift = levels / np.maximum(.0003, previous_bands)
            changed = np.count_nonzero(band_lift > 1.6) >= 2
            if (attack and at - self.last_drop > 8 and self.candidate is None
                    and self.fast_energy > .003 and lift > 1.65 and changed):
                self.candidate = dict(at=at, bed=bed, support=0., attacks=0, roll=rolling)
        candidate = self.candidate
        if candidate:
            if attack: candidate['attacks'] += 1
            candidate['roll'] = candidate['roll'] or rolling
            if rms > max(.002, candidate['bed'] * 1.4):
                candidate['support'] += self.hop / self.sample_rate
            age = at - candidate['at']
            if self.fast_energy < max(.002, candidate['bed'] * 1.35) or age > .6:
                self.candidate = None
            elif age >= .15 and candidate['support'] >= .12 and (candidate['attacks'] >= 2 or candidate['roll']):
                event = dict(kind='drop', at=candidate['at'], confirmed_at=at,
                             strength=self.fast_energy / max(.0005, candidate['bed']))
                self.drops.append(event); self.last_drop_info=event; self.last_drop = at; self.candidate = None
        self.energy_history.append((at, self.fast_energy, levels.copy()))

    def process(self, samples, end_time, bpm=None, sensitivity=50):
        samples = np.asarray(samples, dtype=float).reshape(-1)
        self.drops = []
        if not len(samples) or not np.isfinite(samples).all(): return []
        self.bar_seconds = min(5., max(1., 240 / bpm)) if bpm and 40 <= bpm <= 240 else 2.
        samples = np.concatenate((self.pending, samples))
        length = len(samples) // self.hop * self.hop
        self.pending = samples[length:].copy(); start = end_time - len(samples) / self.sample_rate
        threshold = 1.35 + (100 - sensitivity) * .009
        hits = []
        for offset in range(0, length, self.hop):
            block = samples[offset:offset + self.hop]; self.processed += self.hop
            at = start + offset / self.sample_rate
            self.history[:self.hop] = self.history[self.hop:]; self.history[self.hop:] = block
            spectrum = np.abs(np.fft.rfft(self.history * self.window, n=self.size)) / self.window.sum()
            compressed = np.log1p(spectrum * 100)
            # Compare with neighboring bins too, so vibrato/bin drift in a held
            # note is less likely to become a new percussion attack.
            reference = np.maximum.reduce((self.previous, np.r_[self.previous[0], self.previous[:-1]],
                                           np.r_[self.previous[1:], self.previous[-1]]))
            delta = np.maximum(compressed - reference, 0)
            flux = np.array([float(delta[mask].mean()) if mask.any() else 0. for mask in self.masks])
            levels = np.array([math.sqrt(float(np.sum(spectrum[mask] ** 2))) for mask in self.masks])
            flatness = np.array([float(np.exp(np.log(spectrum[mask] + 1e-12).mean()) /
                                             (spectrum[mask].mean() + 1e-12)) if mask.any() else 0. for mask in self.masks])
            if len(self.flux_history) >= 10:
                history = np.asarray(self.flux_history)
                median = np.median(history, axis=0)
                deviation = np.median(np.abs(history - median), axis=0)
                baseline = np.maximum(.001, median + 3 * deviation)
            else: baseline = np.full(4, .001)
            ratios = flux / baseline
            self.changes = ratios
            rising = levels > self.previous_levels * 1.12
            votes = (ratios > threshold) & (flux > .0015) & rising
            broad = votes & (flatness > .08)
            # Independent body/crack/air changes survive a loud bass bed.
            snare = bool((broad[1] and votes[2]) or (broad[2] and votes[3]))
            hat = bool(broad[3] and ratios[3] > threshold * 1.65 and rising[3])
            kick = bool(votes[0] and ratios[0] > threshold * 1.65 and levels[0] > max(.001, levels[1] * 1.5))
            rms = float(np.sqrt(np.mean(block ** 2)))
            self.level = math.sqrt(float(np.sum(levels[1:] ** 2)))
            attack = (self.processed >= self.sample_rate * .15 and rms > .0002
                      and (snare or hat or kick) and at - self.last_hit >= .045)
            rolling = False
            if attack:
                family = 'snare' if snare else 'hat' if hat else 'kick'
                rolling = self.pattern.add(at, np.minimum(ratios, 20), family)
                strength = float(np.max(ratios[1:]) if family != 'kick' else ratios[0])
                hit = dict(kind='percussion', at=at, strength=strength, family=family, qualified=True,
                           roll=rolling, roll_interval=self.pattern.interval if rolling else None)
                hits.append(hit); self.last_hit = at; self.last_family = family
            self.previous = compressed; self.previous_levels = levels
            self.flux_history.append(flux)
            self.fast_energy = .9 * self.fast_energy + .1 * rms
            self._drop(at, rms, levels, attack, rolling)
        return hits
