import math

def effective_division(bpm, division):
    # Keep hard flashes below three per second while retaining beat multiples.
    return division * max(1, math.ceil(bpm/(180*division)))

def pulse_factor(mode, bpm, now, phase_at, division=1, low=.2):
    division = effective_division(bpm,division) if mode=='beat_flash' else division
    phase = ((now-phase_at)/(60/bpm*division)) % 1
    if mode=='beat_flash': return 1 if phase < .3 else low
    return low + (1-low)*(.5+.5*math.cos(2*math.pi*phase))


def clock_flash_factors(bpm, now, phase_at, count, pattern, division=1, low=.2, *, accent=False):
    high = pulse_factor('beat_flash', bpm, now, phase_at, division, low)
    period = 60/bpm*effective_division(bpm, division)
    if accent:high=1 if ((now-phase_at)/period)%1<.45 else low
    active = math.floor((now-phase_at)/period) % count if count else 0
    return [high if pattern=='together' or i==active else low for i in range(count)]


class HitFlashes:
    """Consume timed attacks once; allow confirmed percussion rolls up to eight/sec."""
    def __init__(self):
        self.reset()

    def reset(self, cursor=0):
        self.cursor = cursor; self.last_flash = -math.inf; self.until = -math.inf
        self.index = 0; self.flashes = 0; self.qualifying_hits = 0
        self.last_attack = -math.inf;self.active={}

    def factors(self, hits, now, count, trigger, pattern, sensitivity=50, division=1, low=.2, *, mode='beat_flash', offset=0, period=.5, accent=False, roll_max_hz=8):
        threshold = 1.3+(100-sensitivity)*.025
        for hit in hits:
            if hit['id'] <= self.cursor: continue
            at=hit['at']+offset
            if at>now:break
            self.cursor = hit['id']
            if not 0 <= now-at <= .2: continue
            if trigger=='bass' and hit['kind']!='bass': continue
            if trigger=='percussion' and hit['kind']!='percussion':continue
            if trigger=='bass_drums' and hit['kind'] not in ('bass','drum'):continue
            if trigger=='frequency' and hit['kind'] not in ('frequency_bass','frequency_mids'):continue
            qualified = trigger=='percussion' and hit.get('qualified',False)
            if (trigger!='frequency' and not qualified and hit['strength'] < threshold) or hit['at']-self.last_attack < .045: continue
            self.last_attack = hit['at']
            self.qualifying_hits += 1
            if (self.qualifying_hits-1) % division: continue
            rolling = trigger=='percussion' and hit.get('roll',False)
            rate = min(8,max(3,roll_max_hz)) if rolling else 3
            gap = 1/rate if mode=='beat_flash' else .08
            # Quantized 10 ms onset timestamps can differ slightly from a roll's
            # beat interval. Delay a near-boundary hit, never exceed the cap.
            tolerance = .015 if rolling and mode=='beat_flash' else 0
            if at-self.last_flash < gap-tolerance-1e-9: continue
            began = max(at,self.last_flash+gap)
            self.index = self.flashes % max(1, count); self.flashes += 1
            self.last_flash = began
            width = min(.15 if accent else .1, max(.05,.4/rate)) if rolling else (.15 if accent else .1)
            self.until = began+width
            if mode=='beat_gentle':
                duration=min(.65,max(.12,period*(1.2 if accent else .85)))
                if rolling and hit.get('roll_interval'):
                    duration=max(.08,min(duration,hit['roll_interval']*.8))
                for i in range(count):
                    if pattern=='together' or i==self.index:self.active[i]=(began,duration)
        if mode=='beat_gentle':
            return [low+(1-low)*(.5+.5*math.cos(math.pi*(now-self.active[i][0])/self.active[i][1]))
                    if i in self.active and 0<=now-self.active[i][0]<self.active[i][1] else low for i in range(count)]
        return [1 if self.last_flash <= now < self.until and (pattern=='together' or i==self.index) else low for i in range(count)]


def clock_gentle_factors(bpm, now, phase_at, count, pattern, division=1, low=.2):
    if pattern=='together' or count<=1:return [pulse_factor('beat_gentle',bpm,now,phase_at,division,low)]*count
    period=60/bpm*division
    cycle=period*max(1,count)
    factors=[]
    for i in range(count):
        distance=abs((now-phase_at-i*period+cycle/2)%cycle-cycle/2)
        envelope=.5+.5*math.cos(math.pi*distance/period) if distance<period else 0
        factors.append(low+(1-low)*envelope)
    return factors


def palette_color(palette, position, offset=0):
    position=max(0,position)+offset
    index=math.floor(position);blend=position-index
    a=palette[index%len(palette)];b=palette[(index+1)%len(palette)]
    return '#' + ''.join(f'{round(int(a[j:j+2],16)*(1-blend)+int(b[j:j+2],16)*blend):02x}' for j in (1,3,5))
