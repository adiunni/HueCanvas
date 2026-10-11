import math

def effective_division(bpm, division, per_minute=180):
    # Keep hard flashes below three per second while retaining beat multiples.
    return division * max(1, math.ceil(bpm/(per_minute*division)))

def gentle_division(bpm, division):
    # Gentle pulses stay at or below four per second (240 BPM): the 20 fps
    # stream needs about five frames to draw a smooth swell.
    return effective_division(bpm, division, 240)

def pulse_factor(mode, bpm, now, phase_at, division=1, low=.2):
    division = effective_division(bpm,division) if mode=='beat_flash' else gentle_division(bpm,division)
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
            if trigger=='bands' and hit['kind']!='band':continue
            if trigger=='rolls' and not (hit['kind']=='percussion' and hit.get('roll')):continue
            qualified = trigger in ('percussion','bands','rolls') and hit.get('qualified',False)
            if (trigger!='frequency' and not qualified and hit['strength'] < threshold) or hit['at']-self.last_attack < .045: continue
            self.last_attack = hit['at']
            self.qualifying_hits += 1
            if (self.qualifying_hits-1) % division: continue
            rolling = trigger in ('percussion','bands','rolls') and hit.get('roll',False)
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
            # Drum confirmation can arrive after the attack; a sharp flash keeps
            # its full width from the first rendered frame. Spacing uses `began`,
            # and gentle pulses still join their envelope partway through.
            self.until = max(began,now)+width
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


class BeatLock:
    """Tempo clock phase-locked to drum hits, for steady pulses that land on the drums.

    The tempo detector supplies the beat period, so pulses never stop for missed
    hits, fills or breakdowns. Kicks (and, more weakly, snares/claps) near a
    predicted beat pull the clock's phase onto them a little at a time, with a
    small bounded tempo trim, so the clock converges without visible jumps.
    Hi-hats and off-beat hits are ignored. If several consistent kicks keep
    landing away from the clock (for example half a beat off), it re-aligns to
    them at once. While drums lock it, the clock learns the detector's usual
    offset from them; without drums for a while it eases back to the detector's
    phase corrected by that offset, so returning drums are still on the clock.
    """
    WINDOW = .2          # hits within ±20% of a period from a beat steer the clock
    KICK_GAIN, SNARE_GAIN = .35, .15
    TRIM_GAIN, TRIM_LIMIT, TRIM_WINDOW = .02, .03, .06
    REALIGN_HITS, REALIGN_SPREAD = 4, .08

    def __init__(self):
        self.reset()

    def reset(self, cursor=0):
        self.cursor = cursor; self.anchor = None; self.base = None; self.period = None
        self.trim = 0.; self.last_lock = -math.inf; self.updated = None; self.strays = []; self.bias = 0.

    def _error(self, at):
        beats = round((at-self.anchor)/self.period)
        return at-(self.anchor+beats*self.period)

    def _retime(self, at):
        """Apply base tempo and trim, keeping the beat nearest at where it is.

        The anchor beat can be minutes old; without this, a 0.05% trim change would
        move the current beat by tens of milliseconds.
        """
        period = self.base*(1+self.trim)
        if self.period:self.anchor += round((at-self.anchor)/self.period)*(self.period-period)
        self.period = period

    def _detector_error(self, now, phase_at):
        # Compare at the detector's beat nearest now, so tempo trim is not magnified
        # by extrapolating far into the past.
        return self._error(phase_at+round((now-phase_at)/self.base)*self.base)

    def update(self, hits, now, bpm, phase_at):
        """Consume new hits; return (bpm, beat time) of the locked clock, or (None, None)."""
        if not bpm or phase_at is None:
            self.anchor = None
            for hit in hits:
                if hit['at'] <= now:self.cursor = max(self.cursor, hit['id'])
            return None, None
        base = 60/bpm
        if self.anchor is None or abs(base-self.base) > self.base*.04:
            # New, doubled or halved tempo (or a new song): start from the detector's phase.
            self.anchor = phase_at; self.base = base; self.trim = 0.; self.strays = []; self.last_lock = now; self.bias = 0.
            self.period = None
        self.base = base; self._retime(now)
        for hit in hits:
            if hit['id'] <= self.cursor:continue
            if hit['at'] > now:break
            self.cursor = hit['id']
            family = hit.get('family')
            if hit.get('kind') != 'percussion' or family not in ('kick', 'snare'):continue
            error = self._error(hit['at'])
            if abs(error) <= self.period*self.WINDOW:
                gain = self.KICK_GAIN if family == 'kick' else self.SNARE_GAIN
                self.anchor += gain*error
                if abs(error) <= self.period*self.TRIM_WINDOW:
                    # Learn a tempo trim only once locked, so the first pull-in does not wind it up.
                    self.trim = max(-self.TRIM_LIMIT, min(self.TRIM_LIMIT, self.trim+self.TRIM_GAIN*error/self.period))
                self._retime(hit['at'])
                self.last_lock = hit['at']
                if family == 'kick':self.strays = []
            elif family == 'kick':
                self.strays = (self.strays+[(hit['at'], error)])[-self.REALIGN_HITS:]
                errors = [e for _, e in self.strays]
                if len(errors) == self.REALIGN_HITS and max(errors)-min(errors) <= self.period*self.REALIGN_SPREAD:
                    # The kicks agree with each other, not with the clock: move onto them.
                    self.anchor += sum(errors)/len(errors); self.strays = []; self.last_lock = hit['at']
        if self.updated is not None:
            elapsed = now-self.updated
            if now-self.last_lock > 8*self.period:
                # A breakdown without drums: ease back to the detector (less its learned
                # offset from the drums) over a few seconds.
                step = min(1., elapsed/3)
                self.anchor += step*(self._detector_error(now, phase_at)-self.bias); self.trim *= 1-step
                self._retime(now)
            else:
                self.bias += min(1., elapsed/2)*(self._detector_error(now, phase_at)-self.bias)
        self.updated = now
        return 60/self.period, self.anchor


def clock_gentle_factors(bpm, now, phase_at, count, pattern, division=1, low=.2):
    if pattern=='together' or count<=1:return [pulse_factor('beat_gentle',bpm,now,phase_at,division,low)]*count
    period=60/bpm*gentle_division(bpm,division)
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
