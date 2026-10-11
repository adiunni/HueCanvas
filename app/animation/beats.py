import math

def effective_division(bpm, division):
    # Keep hard flashes below three per second while retaining beat multiples.
    return division * max(1, math.ceil(bpm/(180*division)))

def pulse_factor(mode, bpm, now, phase_at, division=1, low=.2):
    division = effective_division(bpm,division) if mode=='beat_flash' else division
    phase = ((now-phase_at)/(60/bpm*division)) % 1
    if mode=='beat_flash': return 1 if phase < .3 else low
    return low + (1-low)*(.5+.5*math.cos(2*math.pi*phase))


def clock_flash_factors(bpm, now, phase_at, count, pattern, division=1, low=.2):
    high = pulse_factor('beat_flash', bpm, now, phase_at, division, low)
    period = 60/bpm*effective_division(bpm, division)
    active = math.floor((now-phase_at)/period) % count if count else 0
    return [high if pattern=='together' or i==active else low for i in range(count)]


class HitFlashes:
    """Consume attacks once; never queue old hits or exceed three flashes/sec."""
    def __init__(self):
        self.reset()

    def reset(self, cursor=0):
        self.cursor = cursor; self.last_flash = -math.inf; self.until = -math.inf
        self.index = 0; self.flashes = 0; self.qualifying_hits = 0
        self.last_attack = -math.inf

    def factors(self, hits, now, count, trigger, pattern, sensitivity=50, division=1, low=.2):
        threshold = 1.3+(100-sensitivity)*.025
        for hit in hits:
            if hit['id'] <= self.cursor: continue
            self.cursor = hit['id']
            if not 0 <= now-hit['at'] <= .2: continue
            if trigger=='bass' and hit['kind']!='bass': continue
            if hit['strength'] < threshold or hit['at']-self.last_attack < .06: continue
            self.last_attack = hit['at']
            self.qualifying_hits += 1
            if (self.qualifying_hits-1) % division: continue
            if now-self.last_flash < 1/3: continue
            self.index = self.flashes % max(1, count); self.flashes += 1
            self.last_flash = now; self.until = now+.1
        return [1 if now < self.until and (pattern=='together' or i==self.index) else low for i in range(count)]
