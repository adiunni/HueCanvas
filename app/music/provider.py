from typing import Protocol
class MusicProvider(Protocol):
    async def current_track(self) -> dict: ...
# Future MIDI clock, Link, and audio modulation providers can feed timing to Engine.
