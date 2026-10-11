"""Streaming drum stem: decide whether a detected attack is a drum or a note.

Harmonic–percussive separation by median filtering (Fitzgerald, 2010): tones
are steady across time, drum hits are smooth across frequency. A median over
neighbouring frames estimates the harmonic part; a median over neighbouring
frequency bins estimates the percussive part. The time median needs frames
after the attack, so snare/hat decisions trail the attack by
(ATTACK + LOOKAHEAD) hops, about 50 ms. Rises are measured per bin, so a
decaying note does not hide a drum hit on top of it.

A kick's body is a decaying low tone and looks harmonic to this filter. It is
recognised instead by a jump in low-band energy whose newly risen pitch falls
on every hop, measured from the phase advance of that bin. Bass notes hold or
rise in pitch while the analysis window fills; a legato note change jumps
once without an energy jump. Kick decisions take SWEEP hops, about 40 ms.
"""
import math
from collections import deque
import numpy as np
from scipy.ndimage import median_filter


class DrumStem:
    # Frame k ends k hops after the hop containing the attack starts.
    # Attack frames are 1..ATTACK and sweep frames 2..SWEEP.
    LOOKBACK = 3; LOOKAHEAD = 3; ATTACK = 2; SWEEP = 4
    WINDOW = .021
    PERCUSSIVE = .35; UPPER = .05; FALL = 1.2; JUMP = 2.; CLICK = .6; CLICK_SHARE = .005; LIFT = .04

    def __init__(self, sample_rate, hop):
        self.sample_rate = sample_rate; self.hop = hop
        self.size = max(2 * hop, 2 ** round(math.log2(sample_rate * self.WINDOW)))
        self.window = np.hanning(self.size)
        frequencies = np.fft.rfftfreq(self.size, 1 / sample_rate)
        self.bin_hz = sample_rate / self.size
        self.bins = max(3, int(500 / self.bin_hz) | 1)
        self.upper = (frequencies >= 1000) & (frequencies < min(12000, sample_rate * .48))
        self.low = (frequencies >= 30) & (frequencies < 1000)
        self.low_bins = np.flatnonzero((frequencies >= 30) & (frequencies < 250))
        self.advance = 2 * np.pi * self.low_bins * hop / self.size
        self.history = np.zeros(self.size)
        self.spectra = deque(maxlen=self.LOOKBACK + self.LOOKAHEAD + 1)
        self.frames = deque(maxlen=24)  # (end time, low power, upper power, upper percussive power)
        self.lows = deque(maxlen=24)    # (end time, complex low bins, power above 1 kHz)

    def push(self, block, end_time):
        """Add one hop of samples ending at end_time."""
        self.history[:-self.hop] = self.history[self.hop:]; self.history[-self.hop:] = block
        transform = np.fft.rfft(self.history * self.window); spectrum = np.abs(transform)
        self.lows.append((end_time, transform[self.low_bins], float(np.sum(spectrum[self.upper] ** 2))))
        self.spectra.append((end_time, spectrum, median_filter(spectrum, size=self.bins, mode='nearest')))
        if len(self.spectra) < self.spectra.maxlen: return
        at, centre, percussive = self.spectra[self.LOOKBACK]
        harmonic = np.median([entry[1] for entry in self.spectra], axis=0)
        mask = (percussive ** 2 / (percussive ** 2 + harmonic ** 2 + 1e-18))[self.upper]
        power = centre ** 2; upper = power[self.upper]
        self.frames.append((at, power[self.low], upper, upper * mask ** 2))

    def _between(self, source, at, first, last):
        # Frames whose end lies in hops first..last; tolerant of float rounding.
        hop = self.hop / self.sample_rate
        return [f for f in source if at + (first - .5) * hop <= f[0] < at + (last + .5) * hop]

    def ready(self, at, low):
        source = self.lows if low else self.frames
        last = self.SWEEP if low else self.ATTACK
        return bool(source) and source[-1][0] >= at + (last - .5) * self.hop / self.sample_rate

    def attack(self, at):
        """(percussive share of new energy above 1 kHz, its share of all new energy,
        its size relative to the energy already above 1 kHz)."""
        # Frame 0 counts as attack: in a dense mix the hop flagged as the
        # attack can trail the true onset by one hop.
        before = self._between(self.frames, at, -3, -1); attack = self._between(self.frames, at, 0, self.ATTACK)
        if not before or not attack: return 0., 0., 0.
        # Count only bins that at least double over the loudest earlier frame,
        # so small fluctuations of held notes do not dilute a quiet hit.
        base = [np.max([f[i] for f in before], axis=0) for i in (1, 2, 3)]
        low = upper = percussive = 0.
        for f in attack:
            low += float(np.sum((f[1] - base[0])[f[1] > 2 * base[0]]))
            new = f[2] > 2 * base[1]
            upper += float(np.sum((f[2] - base[1])[new])); percussive += float(np.sum(np.maximum(f[3] - base[2], 0)[new]))
        return (percussive / max(upper, 1e-18), upper / max(low + upper, 1e-18),
                upper / max(float(base[1].sum()) * len(attack), 1e-18))

    def kick(self, at):
        """(fall ratio of the new low energy's pitch, low-band energy jump, low share of the rise).

        Each bin's phase advance gives the frequency of the component near it;
        weighting by newly risen power follows the new sound, not a held note.
        """
        before = self._between(self.lows, at, -2, 0); attack = self._between(self.lows, at, 1, self.SWEEP)
        if not before or len(attack) < self.SWEEP: return 1., 1., 0.
        base = np.max([np.abs(f[1]) ** 2 for f in before], axis=0)
        low = np.mean([np.sum(np.abs(f[1]) ** 2) for f in attack[1:]])
        jump = low / max(float(base.sum()), 1e-18)
        high = max(np.mean([f[2] for f in attack[1:]]) - max(f[2] for f in before), 0)
        dominance = max(low - float(base.sum()), 0) / max(max(low - float(base.sum()), 0) + high, 1e-18)
        frequencies = []
        for previous, current in zip(attack, attack[1:]):
            deviation = (np.angle(current[1]) - np.angle(previous[1]) - self.advance + np.pi) % (2 * np.pi) - np.pi
            instantaneous = (self.low_bins + deviation * self.size / (2 * np.pi * self.hop)) * self.bin_hz
            weight = np.maximum(np.abs(current[1]) ** 2 - base, 0)
            frequencies.append(float(np.sum(weight * instantaneous) / max(float(weight.sum()), 1e-18)))
        # A kick falls on every step; a legato note change jumps once and holds.
        falling = min(frequencies) > 0 and all(a >= b * 1.03 for a, b in zip(frequencies, frequencies[1:]))
        return (frequencies[0] / frequencies[-1] if falling else 1.), float(jump), float(dominance)

    def decide(self, at, low):
        """True for a drum, False for a note, None until enough frames arrive."""
        if not self.ready(at, low): return None
        if low:
            # A kick's new energy sits mostly below 1 kHz; a snare's leaks there.
            fall, jump, dominance = self.kick(at)
            if jump < self.JUMP or dominance < .5: return False
            if fall >= self.FALL: return True
            # A kick landing on a new bass note: accept a strongly percussive
            # click carrying real energy, unlike a sub note's onset splatter.
            if not self.ready(at, False): return None
            share, upper, _ = self.attack(at)
            return share >= self.CLICK and upper >= self.CLICK_SHARE
        share, upper, lift = self.attack(at)
        return share >= self.PERCUSSIVE and upper >= self.UPPER and lift >= self.LIFT
