import math

def frame(settings, palette, elapsed, offset):
    phase = elapsed/settings.speed + offset
    index = int(phase) % len(palette)
    color = palette[index]
    if settings.mode == 'static': color = palette[offset % len(palette)]
    if settings.mode == 'crossfade':
        a, b = palette[index], palette[(index+1)%len(palette)]
        t = phase % 1
        color = '#' + ''.join(f'{round(int(a[i:i+2],16)*(1-t)+int(b[i:i+2],16)*t):02x}' for i in (1,3,5))
    factor = 1
    if settings.mode in ('breathe','bpm'):
        # BPM drives a four-bar ambient envelope, never individual beat flashes.
        period = settings.speed if settings.mode == 'breathe' else max(4, 960/settings.bpm)
        factor = .75 + .25*math.sin(2*math.pi*elapsed/period)
        color = palette[offset % len(palette)]
    return color, factor
