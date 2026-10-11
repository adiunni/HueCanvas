import asyncio
import math
import time
from contextlib import suppress
from app.hue.models import Settings
from app.hue.controller import command
from app.hue.entertainment import EntertainmentSession, STREAM_MODES
from app.palette.convert import hex_to_xy
from app.palette.gamut import map_gamut
from app.animation.modes import frame
from app.animation.beats import pulse_factor, clock_flash_factors, HitFlashes
from app.animation.music_brightness import grouped_beat_factor, energy_factor

class Engine:
    def __init__(self, client):
        self.client=client; self.lights=[]; self.settings=Settings()
        self.palette=['#c65e45','#6d79be','#dfaa65','#456f79']
        self.status='stopped'; self.error=None; self.snapshot=None; self.saved_settings={}
        self.lock=asyncio.Lock(); self.lifecycle_lock=asyncio.Lock(); self.started=time.monotonic()
        self.stream=None; self.stream_factory=EntertainmentSession; self.audio=None
        self.hit_flashes=HitFlashes()
        self.wake=asyncio.Event()
        self.white_task=None; self.rest_brightness={}; self.rest_error=None
        self.rest_lock=asyncio.Lock()

    def uses_audio_hits(self):
        return self.settings.mode=='beat_flash' and self.settings.flash_trigger!='bpm'

    async def refresh(self):
        found=await self.client.discover_lights();old={light.id:light for light in self.lights}
        for light in found:
            if light.id in old:
                for key in ('name','role','enabled'): setattr(light,key,getattr(old[light.id],key))
            elif light.id in self.saved_settings:
                for key,value in self.saved_settings[light.id].model_dump().items(): setattr(light,key,value)
        self.lights=found;self.error=None

    def beat_clock(self):
        if self.settings.bpm_source=='audio':
            bpm=self.audio.current_bpm() if self.audio else None
            return bpm, self.audio.phase_at if bpm else None
        return self.settings.bpm,self.started

    def music_factor(self, now):
        bpm,phase_at=self.beat_clock();low=self.settings.flash_low/100
        if bpm and phase_at is not None: return grouped_beat_factor(bpm,phase_at,now,low)
        energy=self.audio.music_energy() if self.audio and hasattr(self.audio,'music_energy') else None
        return energy_factor(energy,low) if energy is not None else 1

    async def rest_music(self, lights, *, steady=False, force=False):
        async with self.rest_lock:
            now=time.monotonic();factor=1 if steady else self.music_factor(now)
            for light in lights:
                if not light.enabled or not light.dimming: continue
                peak=self.settings.brightness if light.color else self.settings.white_fill
                brightness=max(1,round(peak*factor,1))
                previous=self.rest_brightness.get(light.id)
                if previous and not force:
                    if now-previous[1]<2 or abs(brightness-previous[0])<1: continue
                await self.client.update(light.id,command(light,brightness,duration=1))
                light.on=True;light.brightness=brightness
                self.rest_brightness[light.id]=(brightness,time.monotonic())

    async def white_music(self):
        failures=0
        while True:
            await asyncio.sleep(min(30,.25*2**failures))
            if self.status!='running' or self.settings.mode not in STREAM_MODES: continue
            try:
                await self.rest_music([light for light in self.lights if not light.color],steady=self.settings.white_response=='steady')
                self.rest_error=None;failures=0
            except Exception as exc:
                self.rest_error='White brightness update failed: '+str(exc)
                failures=min(7,failures+1)

    async def apply(self, elapsed=0, *, only_if_running=False):
        async with self.lock:
            if only_if_running and self.status!='running': return
            if self.settings.mode=='music_brightness':
                await self.rest_music(self.lights)
                return
            if self.settings.mode in STREAM_MODES:
                if self.stream is None: raise ValueError('Click Start to initialize Hue Entertainment for beat effects')
                bpm,phase_at=self.beat_clock()
                now=time.monotonic(); low=self.settings.flash_low/100
                colors=sorted((light for light in self.lights if light.enabled and light.color),
                              key=lambda light:({'COLOR_LEFT':0,'COLOR_RIGHT':1}.get(light.role,2),light.id))
                factors=[1]*len(colors)
                if self.uses_audio_hits():
                    if self.audio and self.audio.running:
                        factors=self.hit_flashes.factors(self.audio.recent_hits(self.hit_flashes.cursor),now,len(colors),
                            self.settings.flash_trigger,self.settings.flash_pattern,self.settings.flash_sensitivity,self.settings.beat_every,low)
                elif bpm and phase_at is not None:
                    if self.settings.mode=='beat_flash':
                        factors=clock_flash_factors(bpm,now,phase_at,len(colors),self.settings.flash_pattern,self.settings.beat_every,low)
                    else:
                        factors=[pulse_factor(self.settings.mode,bpm,now,phase_at,self.settings.beat_every,low)]*len(colors)
                values={}
                if self.status=='starting':
                    await self.rest_music([light for light in self.lights if not light.color],steady=self.settings.white_response=='steady',force=True)
                for light,factor in zip(colors,factors):
                    offset=1 if light.role=='COLOR_RIGHT' else 0
                    color=self.palette[offset%len(self.palette)]
                    xy=map_gamut(hex_to_xy(color),light.gamut)
                    brightness=self.settings.brightness*factor
                    values[light.id]=(*xy,brightness/100)
                    light.on=True;light.xy=xy;light.brightness=brightness;light.preview_color=color
                await self.stream.send(values)
                return
            for light in self.lights:
                if not light.enabled: continue
                offset=1 if light.role in ('COLOR_RIGHT','WHITE_RIGHT') else 0
                color,factor=frame(self.settings,self.palette,elapsed,offset)
                if self.settings.mode=='bpm' and self.settings.bpm_source=='audio':
                    bpm,phase_at=self.beat_clock()
                    if bpm and phase_at is not None:
                        factor=.75+.25*math.sin(2*math.pi*(time.monotonic()-phase_at)/max(4,960/bpm))
                    else: factor=1
                brightness=(self.settings.brightness if light.color else self.settings.white_fill)*factor
                payload=command(light,brightness,color,self.settings.transition)
                await self.client.update(light.id,payload)
                light.on=True;light.brightness=brightness;light.preview_color=color if light.color else None
                if 'color' in payload: light.xy=list(payload['color']['xy'].values())

    async def _stop_stream(self):
        if self.white_task:
            self.white_task.cancel()
            with suppress(asyncio.CancelledError): await self.white_task
            self.white_task=None
        async with self.lock:
            if self.stream:
                stream=self.stream;self.stream=None
                await stream.stop()
            self.rest_brightness.clear();self.rest_error=None

    async def start(self):
        async with self.lifecycle_lock:
            self.status='starting'
            try:
                await self._stop_stream()
                await self.refresh()
                if not any(light.enabled for light in self.lights):
                    raise ValueError('No bulbs are enabled. Select Enable bulb or Enable all four in Your lights, then click Start.')
                if self.settings.mode=='music_brightness' and not any(light.enabled and light.dimming for light in self.lights):
                    raise ValueError('Enable at least one dimmable bulb for music brightness.')
                if self.uses_audio_hits() and not (self.audio and self.audio.running):
                    raise ValueError('Start listening to BlackHole before using bass or drum-triggered flashes. A BPM estimate is not required.')
                if self.settings.mode in STREAM_MODES and not self.uses_audio_hits() and self.beat_clock()[0] is None:
                    raise ValueError('Start audio detection and wait for a reliable BPM, or choose Manual BPM before starting beat effects.')
                if self.snapshot is None: self.snapshot=[]
                captured={light.id for light in self.snapshot}
                self.snapshot.extend(light.model_copy(deep=True) for light in self.lights if light.enabled and light.id not in captured)
                self.started=time.monotonic()
                if self.settings.mode in STREAM_MODES:
                    stream=self.stream_factory(self.client)
                    await stream.start(self.lights)
                    self.stream=stream
                self.hit_flashes.reset(getattr(self.audio,'hit_counter',0))
                await self.apply()
            except Exception as exc:
                self.status='stopped';self.error=str(exc)
                try: await self._stop_stream()
                except Exception: pass
                raise
            self.status='running';self.error=None
            if self.settings.mode in STREAM_MODES:
                self.white_task=asyncio.create_task(self.white_music())
            self.wake.set()

    async def pause(self):
        async with self.lifecycle_lock:
            self.status='paused';await self._stop_stream()

    async def stop(self):
        async with self.lifecycle_lock:
            self.status='stopped';await self._stop_stream()

    async def restore(self):
        async with self.lifecycle_lock:
            self.status='stopped';await self._stop_stream()
            async with self.lock:
                for light in self.snapshot or []:
                    payload=command(light,light.brightness,duration=self.settings.transition,on=light.on)
                    if light.color and light.xy: payload['color']={'xy':dict(zip(('x','y'),light.xy))}
                    await self.client.update(light.id,payload)
                self.snapshot=None
            await self.refresh()

    async def run(self):
        failures=0
        while True:
            interval=(.05 if self.settings.mode in STREAM_MODES else .25 if self.settings.mode=='music_brightness' else 2) if self.status=='running' else 2
            try:
                await asyncio.wait_for(self.wake.wait(),timeout=min(30,interval*2**failures))
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
            if self.status!='running' or self.settings.mode=='static': continue
            try:
                await self.apply(time.monotonic()-self.started,only_if_running=True)
                self.error=None;failures=0
            except Exception as exc:
                self.error=str(exc);failures=min(4,failures+1)
                if self.stream:
                    self.status='stopped'
                    try: await self._stop_stream()
                    except Exception: pass
