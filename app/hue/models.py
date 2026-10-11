from typing import Literal
from pydantic import BaseModel, Field, model_validator
from app.audio.bpm import MIN_TEMPO, MAX_TEMPO
Role = Literal['COLOR_LEFT', 'COLOR_RIGHT', 'WHITE_LEFT', 'WHITE_RIGHT']
class Light(BaseModel):
    id: str
    name: str
    owner: str | None = None
    color: bool = False
    dimming: bool = False
    temperature: bool = False
    gamut: list[list[float]] | None = None
    enabled: bool = False
    role: Role | None = None
    on: bool = False
    brightness: float = 50
    xy: list[float] | None = None
    preview_color: str | None = None
class LightSettings(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    enabled: bool
    role: Role | None = None
class Settings(BaseModel):
    mode: Literal['static', 'crossfade', 'rotate', 'breathe', 'bpm', 'beat_gentle', 'beat_flash', 'music_brightness'] = 'static'
    brightness: float = Field(default=60, ge=1, le=100)
    white_fill: float = Field(default=25, ge=1, le=100)
    transition: float = Field(default=3, ge=1, le=30)
    speed: float = Field(default=15, ge=4, le=120)
    bpm: float = Field(default=100, ge=MIN_TEMPO, le=MAX_TEMPO)

    bpm_source: Literal['manual', 'audio'] = 'audio'
    flash_low: float = Field(default=20, ge=0, le=80)
    beat_every: Literal[1, 2, 4] = 1
    flash_trigger: Literal['bpm', 'beat_drums', 'percussion', 'bands', 'frequency', 'bass', 'bass_drums'] = 'bpm'
    flash_pattern: Literal['together', 'staggered'] = 'together'
    roll_max_hz: float = Field(default=8, ge=3, le=8)
    flash_sensitivity: float = Field(default=50, ge=1, le=100)
    drum_isolation: Literal['stems', 'fast'] = 'stems'
    white_response: Literal['music', 'steady'] = 'music'
    white_floor: float = Field(default=20, ge=0, le=80)
    white_pattern: Literal['together', 'alternate'] = 'together'
    pulse_palette: Literal['cycle', 'fixed'] = 'cycle'
    palette_every: Literal[1, 2, 4, 8] = 4
    timing_offset_ms: float = Field(default=0, ge=0, le=250)
    drop_response: Literal['accent', 'off'] = 'accent'
    palette_size: int = Field(default=4, ge=2, le=6)
    bass_threshold_db: float = Field(default=-15, ge=-90, le=0)
    bass_fallback_db: float = Field(default=-70, ge=-90, le=0)
    mids_threshold_db: float = Field(default=-20, ge=-90, le=0)

    @model_validator(mode='after')
    def ordered_frequency_thresholds(self):
        if self.bass_fallback_db >= self.bass_threshold_db:
            raise ValueError('Bass fallback must be below the bass trigger threshold')
        return self
