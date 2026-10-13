import time
from collections import deque
from app.events.models import EventOptions, Preset
from app.events.timing import CueClock
from app.events.effects import envelope


class EventRuntime:
    def __init__(self):
        self.options = EventOptions()
        self.clock = CueClock()
        self.cues = []
        self.active = None
        self.resting = False
        self.paused_at = None
        self.history = deque(maxlen=100)
        self.live_candidates = deque(maxlen=100)
        self.last_live = {}
        self.last_epoch = 0
        self.processing_ms = 0.
        self.follow_paused = False
        self.error = None

    def transport(self, update, now):
        changed = self.clock.update(update, now)
        if changed:
            self.active = None
            self.resting = False
            self.last_live.clear()
            self.live_candidates.clear()
            self.last_epoch = self.clock.epoch
        if update.playing:
            self.resume(now)
        else:
            self.pause(now)
        return changed

    def pause(self, now):
        if self.paused_at is None:
            self.paused_at = now

    def resume(self, now):
        if self.paused_at is not None:
            if self.active:
                cue, start = self.active
                self.active = cue, start + now - self.paused_at
            self.paused_at = None

    def clear(self):
        self.active = None
        self.resting = False

    def trigger(self, cue, now, age=0., *, preview=False):
        self.active = cue.model_copy(deep=True), now - age
        self.resting = False
        self.history.append({'id': cue.id, 'type': cue.type.value, 'position': cue.position,
                             'at': now, 'late_ms': round(max(0, age) * 1000, 1), 'preview': preview})

    def tick(self, now):
        if not self.options.enabled or self.paused_at is not None:
            return
        due = self.clock.poll(self.cues, now, self.options.output_latency_ms / 1000, self.options.confidence)
        if self.clock.epoch != self.last_epoch:
            self.clear()
            self.last_epoch = self.clock.epoch
        for cue, age in due:
            self.trigger(cue, now, age)

    def sample(self, now, *, slow=False, bpm=None):
        if not self.options.enabled:
            return None
        if not self.active:
            return {'level': self.options.ambient_level, 'motion': 0., 'done': True} if self.resting else None
        cue, start = self.active
        age = (self.paused_at if self.paused_at is not None else now) - start
        if age < 0 and cue.source == 'live':return None
        result = envelope(cue, age, slow=slow, reduced=self.options.reduced_motion,
                          ambient=self.options.ambient_level, bpm=bpm)
        # Keep the terminal target, so the next frame cannot jump back up.
        if result['done'] and cue.effect in (Preset.FINAL_RESOLUTION, Preset.OUTRO_FADE):
            self.resting = True
        if result['done'] and not self.resting:
            return None
        return result

    def live(self, cue, now, bpm=None, phase=None, *, can_trigger=True):
        previous = self.last_live.get(cue.type, -float('inf'))
        if cue.detected_at is None or cue.detected_at - previous < 8:
            return
        self.last_live[cue.type] = cue.detected_at
        self.live_candidates.append(cue)
        if not (self.options.enabled and self.options.live_trigger and can_trigger and cue.confidence >= self.options.confidence):
            return
        # A matching accepted scheduled cue owns this boundary.
        if any(c.enabled and c.review == 'accepted' and c.type == cue.type and abs(c.position - cue.position) < 2 for c in self.cues):
            return
        delay = self.options.live_delay_ms / 1000
        if self.options.beat_align and bpm and phase is not None:
            period = 60 / bpm
            delay += (phase - now - delay) % period
        # Reactive effects begin at confirmation (+ optional forward beat alignment).
        # Live events have no pre-impact dimming and no negative-latency prediction.
        cue = cue.model_copy(deep=True)
        cue.envelope.pre_dim = 0
        self.resume(now)
        self.trigger(cue, now, -delay)

    def status(self, now=None):
        now = time.monotonic() if now is None else now
        return {'options': self.options.model_dump(), 'transport': {
                    **self.clock.transport.model_dump(), 'position': self.clock.position(now)},
                'history': list(self.history), 'live_candidates': [c.model_dump(mode='json') for c in self.live_candidates],
                'processing_ms': self.processing_ms, 'error': self.error}
