"""Causal section evidence. A quiet tail is a candidate ending, never foresight."""
import math
from collections import deque
import numpy as np
from app.events.models import ALGORITHM_VERSION, Cue, DEFAULT_EFFECT, EventType


class LiveEvents:
    def __init__(self):
        self.rows = deque(maxlen=160)  # 40 s at the worker's 4 Hz cadence
        self.pending = None
        self.accent = None
        self.armed = True
        self.last = {}
        self.building = False
        self.last_at = None

    def feed(self, at, rms, bass, flux, density, *, now, track_id, position, threshold=.7):
        if self.last_at is not None and (at <= self.last_at or at - self.last_at > 1):
            if at <= self.last_at:
                return []
            self.rows.clear(); self.pending = None; self.accent = None; self.armed = True
        self.last_at = at
        output = []
        history = [row for row in self.rows if 1 <= at - row[0] <= 9]
        attack=max(flux,rms-self.rows[-1][1] if self.rows else 0.)
        measurements = dict(rms=rms, bass=bass, energy_attack=attack, onset_density=density,feature_window_seconds=.25)

        def emit(kind, onset, confidence, support):
            if confidence < threshold or onset - self.last.get(kind, -math.inf) < 12:
                return
            self.last[kind] = onset
            output.append(Cue(track_id=track_id, type=kind, position=max(0, position - (at - onset)),
                              source='live', review='pending', confidence=confidence,
                              algorithm_version=ALGORITHM_VERSION + '-live', effect=DEFAULT_EFFECT[EventType(kind)],
                              detected_at=onset, detection_delay=max(0, now - onset),
                              features={**measurements, **support, 'confirmation_seconds': at - onset}))

        if len(history) >= 16:
            base = max(.0001, float(np.median([r[1] for r in history])))
            bass_base = max(.0001, float(np.median([r[2] for r in history])))
            mad = float(np.median(abs(np.array([r[1] for r in history]) - base)))
            lift = rms / base
            support = dict(energy_ratio=lift, bass_return=bass / bass_base, baseline_mad=mad)
            if lift < 1.25:
                self.armed = True
                self.pending = None
            if self.armed and lift > 1.8 and rms > max(.002, base + 3 * mad) and bass / bass_base > 1.4 and density >= 1:
                if self.pending is None:
                    self.pending = at
                if at - self.pending >= .75:
                    emit('DROP', self.pending, min(.93, .64 + .1 * math.log2(max(1, lift))), support)
                    if self.building:
                        emit('BUILD_END', self.pending, .75, support)
                    self.building = False; self.armed = False; self.pending = None
            elif self.pending is not None:
                self.pending = None
            recent = [r for r in self.rows if at - r[0] <= 4]
            if len(recent) >= 12:
                levels = np.array([r[1] for r in recent])
                rising = np.mean(np.diff(levels) > 0) >= .65
                if not self.building and rising and lift > 1.4 and lift < 2.5:
                    emit('BUILD_START', recent[0][0], .72, support)
                    self.building = True
                if rms < base * .45 and np.mean(levels[-8:]) < base * .5 and base > .002:
                    emit('BREAKDOWN', recent[-8][0], .72, support)
                if np.mean(np.diff(levels) < 0) >= .7 and lift < .5 and base > .002:
                    emit('OUTRO_START', recent[0][0], .71, support)
                if rms < .00005 and max(levels[-8:]) < .00005 and base > .002:
                    emit('TRACK_END', recent[-8][0], .7, {**support, 'provisional_ending': 1.})
            # Final accents need retrospectively observed reduction, not just a
            # loud frame. They remain provisional without a known track timeline.
            if lift > 3.5 and rms > .01 and attack > .002:
                self.accent = (at, rms)
            if self.accent:
                hit_at, peak = self.accent
                tail = [r for r in self.rows if r[0] >= hit_at + .5]
                if at - hit_at >= 2 and tail:
                    if max(r[1] for r in tail) < peak * .3 and density < .7:
                        emit('FINAL_IMPACT', hit_at, .72, {**support, 'tail_ratio': rms / peak, 'provisional_ending': 1.})
                    self.accent = None
        self.rows.append((at, rms, bass, flux, density))
        return output


def live_measurements(audio):
    """Read a short existing capture window; never open a stream or save PCM."""
    with audio.lock:
        if not audio.running or not audio.frames:
            return None
        end, generation, rate = audio.end_time, audio.generation, audio.sample_rate
        blocks, count = [], 0
        for block in reversed(audio.buffer):
            blocks.append(block); count += len(block)
            if count >= rate // 4:
                break
        density = sum(1 for h in audio.hits if h['kind'] == 'percussion' and end - h['at'] <= 1.5) / 1.5
    samples = np.concatenate(list(reversed(blocks)))[-rate // 4:]
    if len(samples)<32:
        return None
    transform = abs(np.fft.rfft(samples * np.hanning(len(samples)))) / max(1, len(samples))
    frequencies = np.fft.rfftfreq(len(samples), 1 / rate)
    bass = float(np.sqrt(np.sum(transform[(frequencies >= 25) & (frequencies < 200)] ** 2)))
    rms = float(np.sqrt(np.mean(samples ** 2)))
    # Within-window attack contrast supplements the existing confirmed hit density.
    middle = len(samples) // 2
    flux = max(0., float(np.sqrt(np.mean(samples[middle:] ** 2)) - np.sqrt(np.mean(samples[:middle] ** 2))))
    return end, generation, rms, bass, flux, density
