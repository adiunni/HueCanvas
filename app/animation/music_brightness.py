"""Music brightness envelopes suited to slow REST dimming lamps."""
import math


def grouped_beat_factor(bpm, phase_at, now, low=.2):
    # Whole groups of beats, with at least two seconds between targets.
    beats = max(4, 2**math.ceil(math.log2(max(1, 2*bpm/60))))
    step = beats*60/bpm
    return 1 if math.floor((now-phase_at)/step)%2==0 else low


def energy_factor(rms, low=.2):
    return low+(1-low)*min(1, math.sqrt(max(0,rms)/.08))


def grouped_beat_factors(bpm, phase_at, now, count, pattern, low=.2):
    beats=max(4,2**math.ceil(math.log2(max(1,2*bpm/60))))
    step=beats*60/bpm
    return [grouped_beat_factor(bpm,phase_at+(i*step if pattern=='staggered' else 0),now,low) for i in range(count)]
