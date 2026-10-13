"""Pure effect envelopes. Output is bounded by the dashboard brightness ceilings."""
import math
from app.events.models import Preset


def smooth(value):
    value = min(1., max(0., value))
    return value * value * (3 - 2 * value)


def envelope(cue, age, *, slow=False, reduced=False, ambient=.3, bpm=None):
    e = cue.envelope
    attack = max(e.attack, 1 if slow or reduced else .05)
    hold = max(e.hold, 2 if slow else 0)
    decay = max(e.decay, 4 if slow or reduced else 1)
    strength = e.intensity * (.6 if reduced else 1)
    if age < 0:
        amount = smooth((age + e.pre_dim) / max(e.pre_dim, .01))
        return {'level': .65 * (1 - .55 * strength * amount), 'motion': 0., 'done': False}
    if cue.effect in (Preset.FINAL_RESOLUTION, Preset.OUTRO_FADE):
        # Intensity controls how quickly the fade develops; its endpoint remains
        # the configured ambient scene even at low intensity.
        progress = smooth((age / (attack + hold + decay)) ** (2 - e.intensity))
        return {'level': .65 + (ambient - .65) * progress, 'motion': 0., 'done': progress >= 1}
    if cue.effect == Preset.BUILD_RAMP:
        ramp = max(attack + hold, 2)
        amount = smooth(age / ramp)
        if age > ramp:
            amount *= 1 - smooth((age - ramp) / decay)
    else:
        amount = smooth(age / attack) if age < attack else 1 - smooth((age - attack - hold) / decay)
    motion = 0.
    if cue.effect == Preset.DROP_RELEASE and not slow and not reduced:
        # Smooth, shallow motion. No strobe or randomly selected colors.
        period = max(.5, 60 / (bpm or 120))
        amount *= .85 + .15 * math.cos(2 * math.pi * age / period)
        motion = .5 * strength * max(0, amount)
    level = .65 + .35 * strength * amount
    rise = max(attack + hold,2) if cue.effect == Preset.BUILD_RAMP else attack
    if e.pre_dim and age < rise:
        level -= .65 * .55 * strength * (1 - smooth(age/rise))
    finish = max(attack+hold,2)+decay if cue.effect == Preset.BUILD_RAMP else attack+hold+decay
    return {'level': level, 'motion': motion, 'done': age >= finish}
