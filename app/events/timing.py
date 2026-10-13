"""Absolute monotonic anchors, never accumulated timer ticks."""
import math
from app.events.models import TransportUpdate


class CueClock:
    def __init__(self):
        self.transport = TransportUpdate(track_id='manual', position=0)
        self.anchor = 0.
        self.fired = set()
        self.previous = -1e-9
        self.epoch = 0
        self.loop_iteration = 0

    def raw_position(self, now):
        t = self.transport
        return t.position + max(0, now - self.anchor) * t.rate if t.playing else t.position

    def position(self, now):
        raw = self.raw_position(now)
        t = self.transport
        if t.loop_end is not None and raw >= t.loop_end:
            return t.loop_start + (raw - t.loop_start) % (t.loop_end - t.loop_start)
        return raw

    def update(self, update, now):
        old = self.transport
        discontinuity = (update.track_id != old.track_id or update.seek or
                         abs(update.position - self.position(now)) > .75 or
                         (old.loop_start, old.loop_end) != (update.loop_start, update.loop_end))
        if discontinuity:
            self.epoch += 1
            self.fired.clear()
            self.previous = update.position - 1e-9
        self.transport = update.model_copy(deep=True)
        self.anchor = now
        self.loop_iteration = 0
        return discontinuity

    def poll(self, cues, now, advance=0, threshold=.7):
        t = self.transport
        if not t.playing:
            return []
        raw = self.raw_position(now)
        iteration = 0
        if t.loop_end is not None and raw >= t.loop_end:
            iteration = math.floor((raw - t.loop_start) / (t.loop_end - t.loop_start))
        position = self.position(now)
        due = []

        def collect(start, stop, reference, looped=False):
            found=[]
            for cue in sorted(cues,key=lambda c:c.position):
                if (cue.track_id != t.track_id or not cue.enabled or cue.review != 'accepted'
                        or cue.id in self.fired or (cue.confidence is not None and cue.confidence < threshold and not cue.override)):
                    continue
                if t.loop_end is not None and (cue.position>=t.loop_end or (looped and cue.position<t.loop_start)):
                    continue
                trigger=max(t.loop_start if looped else 0, cue.position-advance*t.rate-cue.envelope.pre_dim*t.rate)
                # Joining inside a cue's pre-dim window still arms its future
                # impact. Seeking beyond the impact never replays it.
                if trigger<=start<=cue.position-advance*t.rate:
                    trigger=start+1e-10
                if start<trigger<=stop:
                    self.fired.add(cue.id)
                    if reference-trigger<=max(1,t.rate):
                        found.append((cue,(reference-cue.position)/t.rate+advance))
            return found

        if iteration != self.loop_iteration:
            # A poll straddling the wrap must still deliver the last fraction of
            # the old pass. Multiple missed passes are deliberately not replayed.
            if iteration==self.loop_iteration+1:
                due.extend(collect(self.previous,t.loop_end,t.loop_end+position-t.loop_start,self.loop_iteration>0))
            self.loop_iteration = iteration
            self.epoch += 1
            self.fired.clear()
            self.previous = t.loop_start - 1e-9
        due.extend(collect(self.previous,position,position,iteration>0))
        self.previous = position
        return due
