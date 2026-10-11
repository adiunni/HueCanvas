"""Causal multiband changes, repeated attacks and musical-section contrast.

This estimates percussion patterns in a mix; it does not separate instruments.
"""
import math
from collections import deque
import numpy as np
from app.audio.stems import DrumStem
from app.audio.bpm import MIN_TEMPO, MAX_TEMPO


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
            compatible = family == previous[2] or {family, previous[2]} <= {'snare', 'hat'}
            if not .045 <= gap <= .26 or not similar or not compatible:
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


class BandLead:
    """Choose whether bass or mid-band changes drive the lights.

    Bass leads while it keeps producing attacks. When it stops (a bass-less
    drop, a filtered build-up) and the mids keep changing, mids take over;
    bass reclaims the lead once its attacks return.
    """
    WINDOW = 1.5; HOLD = .5

    def __init__(self):
        self.band = 'bass'; self.since = -math.inf; self.locked_until = -math.inf
        self.onsets = {'bass': deque(), 'mids': deque()}
        self.levels = {'bass': 0., 'mids': 0.}

    def update(self, at, bass, mids, bass_level, mids_level):
        for band, onset, level in (('bass', bass, bass_level), ('mids', mids, mids_level)):
            times = self.onsets[band]
            if onset: times.append(at)
            while times and at - times[0] > self.WINDOW: times.popleft()
            self.levels[band] = .98 * self.levels[band] + .02 * level
        if at < self.locked_until or at - self.since < self.HOLD: return self.band
        bass = self.onsets['bass']
        # Low-band attacks far below the mids are leakage, not a bass line.
        loud = self.levels['bass'] >= self.levels['mids'] * .25
        if self.band == 'bass':
            # Leave bass once its next expected attack is clearly missed.
            gaps = np.diff(bass)
            limit = float(np.clip(1.6 * np.median(gaps), .6, self.WINDOW)) if len(gaps) else self.WINDOW
            if (not bass or at - bass[-1] > limit or not loud) and len(self.onsets['mids']) >= 2:
                self.band = 'mids'; self.since = at
        elif len(bass) >= 2 and loud:
            self.band = 'bass'; self.since = at
        return self.band

    def force(self, band, at, hold=2.):
        if band != self.band: self.since = at
        self.band = band; self.locked_until = at + hold


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
        self.lead = BandLead(); self.band_hits = []; self.last_band = {'bass': -math.inf, 'mids': -math.inf}
        self.band_levels = deque([np.zeros(3)], maxlen=6)
        self.stem = DrumStem(sample_rate, self.hop); self.queue = deque(); self.last_candidate = -math.inf

    def status(self, now):
        return dict(self.pattern.status(now), band_change=dict(zip(self.BAND_NAMES, self.changes.tolist())),
                    last_family=self.last_family if now - self.last_hit < .75 else None,
                    comparison_seconds=round(self.bar_seconds, 2), lead_band=self.lead.band,
                    drop_active=bool(self.last_drop_info and 0<=now-self.last_drop_info['confirmed_at']<2))

    def _drop(self, at, rms, levels, attack, confirmed, rolling):
        # `attack` is this hop's unconfirmed candidate; `confirmed` lists the
        # times of drum hits the stem confirmed during this hop.
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
                self.candidate = dict(at=at, bed=bed, support=0., attacks=0, roll=rolling, bass_lift=float(band_lift[0]))
        candidate = self.candidate
        if candidate:
            candidate['attacks'] += sum(time >= candidate['at'] - 1e-9 for time in confirmed)
            candidate['roll'] = candidate['roll'] or rolling
            if rms > max(.002, candidate['bed'] * 1.4):
                candidate['support'] += self.hop / self.sample_rate
            age = at - candidate['at']
            if self.fast_energy < max(.002, candidate['bed'] * 1.35) or age > .6:
                self.candidate = None
            elif age >= .15 and candidate['support'] >= .12 and (candidate['attacks'] >= 2 or candidate['roll']):
                # A drop without a bass lift hands the lights to the mids.
                band = 'bass' if candidate['bass_lift'] > 1.6 else 'mids'
                event = dict(kind='drop', at=candidate['at'], confirmed_at=at, band=band,
                             strength=self.fast_energy / max(.0005, candidate['bed']))
                self.lead.force(band, at)
                self.drops.append(event); self.last_drop_info=event; self.last_drop = at; self.candidate = None
        self.energy_history.append((at, self.fast_energy, levels.copy()))

    def _resolve(self, isolate):
        """Release queued candidates in order once the drum stem can judge them."""
        released = []
        while self.queue:
            candidate = self.queue[0]
            if isolate:
                verdict = {test: self.stem.decide(candidate['at'], test == 'low') for test in candidate['tests']}
                if None in verdict.values(): break
            else:
                family = candidate['family']
                verdict = {'low': family in (None, 'kick'), 'upper': family != 'kick'}
            self.queue.popleft(); candidate['verdict'] = verdict; candidate['isolated'] = isolate
            released.append(candidate)
        return released

    def process(self, samples, end_time, bpm=None, sensitivity=50, isolate=True):
        """Return confirmed percussion hits. With `isolate`, attacks wait for the
        drum stem (about 40–50 ms) and notes are discarded."""
        samples = np.asarray(samples, dtype=float).reshape(-1)
        self.drops = []; self.band_hits = []
        if not len(samples) or not np.isfinite(samples).all(): return []
        self.bar_seconds = min(5., max(1., 240 / bpm)) if bpm and MIN_TEMPO <= bpm <= MAX_TEMPO else 2.
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
                      and (snare or hat or kick) and at - self.last_candidate >= .045)
            # Band changes for the bass/mids trigger, reusing the per-band votes.
            # A change must also be a new peak above the last 60 ms, so a
            # decaying kick's window-phase wobble does not retrigger.
            ready = self.processed >= self.sample_rate * .15 and rms > .0002
            peak = levels[:3] > np.max(self.band_levels, axis=0) * 1.4
            onsets = {'bass': ready and bool(votes[0] and peak[0]) and levels[0] > levels[1],
                      'mids': ready and bool(np.any(votes[1:3] & peak[1:3]))}
            self.band_levels.append(levels[:3].copy())
            for band in onsets:
                onsets[band] = onsets[band] and at - self.last_band[band] >= .045
                if onsets[band]: self.last_band[band] = at
            if attack or any(onsets.values()):
                # Attacks take both tests: a kick's click also moves the upper
                # bands. Bass changes are judged as low drums, mids changes by
                # their percussive share above 300 Hz.
                family = ('snare' if snare else 'hat' if hat else 'kick') if attack else None
                tests = {'low', 'upper'} if family else set()
                tests |= {'low'} if onsets['bass'] else set()
                tests |= {'upper'} if onsets['mids'] else set()
                self.queue.append(dict(at=at, family=family, flux=flux, tests=tests, onsets=onsets,
                                       strength=float(ratios[0] if family == 'kick' else np.max(ratios[1:])),
                                       bass=float(ratios[0]), mids=float(max(ratios[1], ratios[2]))))
                if attack: self.last_candidate = at
            self.stem.push(block, at + self.hop / self.sample_rate)
            accepted = {'bass': False, 'mids': False}; confirmed = []; rolling = False
            released = self._resolve(isolate)
            for candidate in released:
                verdict = candidate['verdict']; family = candidate['family']
                rolled = False
                if family and (verdict['low'] or verdict['upper']):
                    # The stem's verdict overrides the band-vote label.
                    if candidate['isolated']: family = 'kick' if verdict['low'] else 'snare' if family == 'kick' else family
                    rolled = self.pattern.add(candidate['at'], candidate['flux'], family)
                    hits.append(dict(kind='percussion', at=candidate['at'], strength=candidate['strength'],
                                     family=family, qualified=True, roll=rolled,
                                     roll_interval=self.pattern.interval if rolled else None))
                    self.last_hit = candidate['at']; self.last_family = family
                    confirmed.append(candidate['at']); rolling = rolling or rolled
                candidate['roll'] = rolled
                if candidate['isolated']:
                    # Stem split: a confirmed kick drives bass, other drums the mids.
                    kick = verdict.get('low', False) and (family == 'kick' or candidate['onsets']['bass'])
                    candidate['accepted'] = {'bass': kick, 'mids': not kick and verdict.get('upper', False)
                                             and (bool(family) or candidate['onsets']['mids'])}
                else: candidate['accepted'] = dict(candidate['onsets'])
                for band in accepted: accepted[band] = accepted[band] or candidate['accepted'][band]
            lead = self.lead.update(at, accepted['bass'], accepted['mids'], levels[0], math.hypot(levels[1], levels[2]))
            for candidate in released:
                if candidate['accepted'][lead]:
                    self.band_hits.append(dict(kind='band', band=lead, at=candidate['at'], qualified=True,
                                               strength=candidate[lead], roll=candidate['roll'],
                                               roll_interval=self.pattern.interval if candidate['roll'] else None))
            self.previous = compressed; self.previous_levels = levels
            self.flux_history.append(flux)
            self.fast_energy = .9 * self.fast_energy + .1 * rms
            self._drop(at, rms, levels, attack, confirmed, rolling)
        return hits
