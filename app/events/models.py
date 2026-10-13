from enum import Enum
from typing import Literal
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, model_validator

ALGORITHM_VERSION = 'sections-1.0'


class Model(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra='forbid')


class EventType(str, Enum):
    DROP = 'DROP'
    BUILD_START = 'BUILD_START'
    BUILD_END = 'BUILD_END'
    BREAKDOWN = 'BREAKDOWN'
    FINAL_IMPACT = 'FINAL_IMPACT'
    OUTRO_START = 'OUTRO_START'
    TRACK_END = 'TRACK_END'


class Preset(str, Enum):
    CINEMATIC_IMPACT = 'CINEMATIC_IMPACT'
    DROP_RELEASE = 'DROP_RELEASE'
    BUILD_RAMP = 'BUILD_RAMP'
    FINAL_RESOLUTION = 'FINAL_RESOLUTION'
    OUTRO_FADE = 'OUTRO_FADE'


DEFAULT_EFFECT = {
    EventType.DROP: Preset.DROP_RELEASE,
    EventType.BUILD_START: Preset.BUILD_RAMP,
    EventType.BUILD_END: Preset.DROP_RELEASE,
    EventType.BREAKDOWN: Preset.OUTRO_FADE,
    EventType.FINAL_IMPACT: Preset.CINEMATIC_IMPACT,
    EventType.OUTRO_START: Preset.OUTRO_FADE,
    EventType.TRACK_END: Preset.FINAL_RESOLUTION,
}


class Envelope(Model):
    attack: float = Field(default=.25, ge=.05, le=30)
    hold: float = Field(default=.6, ge=0, le=30)
    decay: float = Field(default=5, ge=1, le=120)
    intensity: float = Field(default=.65, ge=0, le=1)
    pre_dim: float = Field(default=0, ge=0, le=4)


class Cue(Model):
    schema_version: Literal[1] = 1
    id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=100)
    track_id: str = Field(min_length=1, max_length=300)
    type: EventType
    position: float = Field(ge=0, le=86400, description='Seconds from track start')
    source: Literal['manual', 'offline', 'live'] = 'manual'
    confidence: float | None = Field(default=None, ge=0, le=1)
    algorithm_version: str = Field(default='manual-1', max_length=100)
    features: dict[str, float] = Field(default_factory=dict)
    effect: Preset = Preset.CINEMATIC_IMPACT
    envelope: Envelope = Field(default_factory=Envelope)
    enabled: bool = True
    review: Literal['pending', 'accepted', 'rejected'] = 'accepted'
    user_edited: bool = False
    original_position: float | None = Field(default=None, ge=0, le=86400)
    override: bool = False
    detected_at: float | None = None  # monotonic input clock, for live events only
    detection_delay: float | None = Field(default=None, ge=0)


class EventOptions(Model):
    enabled: bool = False
    output: Literal['rest', 'entertainment'] = 'rest'
    reduced_motion: bool = True
    ambient_level: float = Field(default=.3, ge=.05, le=1)
    confidence: float = Field(default=.7, ge=.1, le=1)
    live_enabled: bool = False
    live_trigger: bool = False
    beat_align: bool = False
    # Positive advances known cues; it cannot advance an unknown live event.
    output_latency_ms: float = Field(default=0, ge=0, le=1000)
    live_delay_ms: float = Field(default=0, ge=0, le=1000)
    follow_music: bool = False


class TransportUpdate(Model):
    track_id: str = Field(min_length=1, max_length=300)
    position: float = Field(ge=0, le=86400)
    playing: bool = False
    rate: float = Field(default=1, ge=.25, le=4)
    seek: bool = False
    loop_start: float | None = Field(default=None, ge=0, le=86400)
    loop_end: float | None = Field(default=None, ge=0, le=86400)

    @model_validator(mode='after')
    def valid_loop(self):
        if (self.loop_start is None) != (self.loop_end is None):
            raise ValueError('Supply both loop boundaries')
        if self.loop_start is not None and self.loop_end - self.loop_start < 1:
            raise ValueError('A cue loop must be at least one second long')
        return self
