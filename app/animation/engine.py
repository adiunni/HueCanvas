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
from app.animation.beats import clock_gentle_factors, clock_flash_factors, HitFlashes, palette_color
from app.animation.music_brightness import grouped_beat_factor, grouped_beat_factors, energy_factor

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
        self.silence_since=None;self.sound_since=None;self.audio_checked_at=-float('inf')
        self.waiting_restored=True;self.playback_message=None
        self.drop_cursor=0;self.palette_shift=0;self.drop_until=0

    def uses_audio_hits(self):
        return self.settings.mode in STREAM_MODES and self.settings.flash_trigger!='bpm'

    def follows_audio(self):
        return bool(self.audio and self.audio.running and hasattr(self.audio,'music_energy'))

    def white_music_active(self):
        return self.settings.mode in STREAM_MODES or (
            self.settings.mode!='music_brightness' and self.settings.white_response=='music' and self.follows_audio())

    def reset_audio_wait(self):
        self.silence_since=None;self.sound_since=None;self.audio_checked_at=-float('inf')
        self.playback_message=None;self.waiting_restored=True

    async def check_audio(self):
        # RMS covers the last second. Add two seconds of silence so brief musical
        # gaps do not release the stream or repeatedly restore the bulbs.
        now=time.monotonic()
        if now-self.audio_checked_at<.25: return
        self.audio_checked_at=now
        async with self.lifecycle_lock:
            if self.status not in ('running','waiting_audio') or not self.follows_audio():
                self.silence_since=None;self.sound_since=None
                return
            energy=self.audio.music_energy()
            if self.status=='running':
                if energy is not None and energy>=.00005:
                    self.silence_since=None
                    return
                if self.silence_since is None:self.silence_since=now
                if now-self.silence_since<2:return
                self.status='waiting_audio';self.waiting_restored=False;self.sound_since=None
            if not self.waiting_restored:
                await self._stop_stream()
                await self._restore_snapshot(clear=False)
                self.waiting_restored=True
                self.playback_message='Lights restored after silence. Waiting for music to resume.'
            if energy is None or energy<.00015:
                self.sound_since=None
                return
            if self.sound_since is None:self.sound_since=now
            if now-self.sound_since<.5:return
            if self.settings.mode in STREAM_MODES and not self.uses_audio_hits() and self.beat_clock()[0] is None:
                self.playback_message='Music resumed. Waiting for a reliable BPM before restarting the beat effect.'
                return
            await self._start()

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
        if self.uses_audio_hits() and self.follows_audio():
            bpm=self.audio.current_bpm();phase_at=self.audio.phase_at
        if bpm and phase_at is not None: return grouped_beat_factor(bpm,phase_at,now,low)
        energy=self.audio.music_energy() if self.audio and hasattr(self.audio,'music_energy') else None
        return energy_factor(energy,low) if energy is not None else 1

    def collect_drops(self,now):
        if not self.audio or not hasattr(self.audio,'recent_drops'):return
        for drop in self.audio.recent_drops(self.drop_cursor):
            self.drop_cursor=drop['id']
            if self.settings.drop_response=='accent' and 0<=now-drop['at']<1:
                self.palette_shift+=1;self.drop_until=now+.6

    async def rest_music(self, lights, *, steady=False, force=False):
        async with self.rest_lock:
            now=time.monotonic();factor=1 if steady else self.music_factor(now)
            whites=sorted((l for l in lights if not l.color and l.enabled),
                key=lambda l:({'WHITE_LEFT':0,'WHITE_RIGHT':1}.get(l.role,2),l.id))
            white_factors={}
            bpm,phase_at=self.beat_clock()
            if self.uses_audio_hits() and self.follows_audio():
                bpm=self.audio.current_bpm();phase_at=self.audio.phase_at
            if not steady and bpm and phase_at is not None and self.settings.flash_pattern=='staggered':
                white_factors=dict(zip((l.id for l in whites),grouped_beat_factors(bpm,phase_at,now,len(whites),'staggered',self.settings.flash_low/100)))
            for light in lights:
                if not light.enabled or not light.dimming: continue
                peak=self.settings.brightness if light.color else self.settings.white_fill
                brightness=max(1,round(peak*white_factors.get(light.id,factor),1))
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
            if self.status!='running' or not self.white_music_active(): continue
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
                self.collect_drops(now)
                offset_seconds=self.settings.timing_offset_ms/1000
                if phase_at is not None:phase_at+=offset_seconds
                colors=sorted((light for light in self.lights if light.enabled and light.color),
                              key=lambda light:({'COLOR_LEFT':0,'COLOR_RIGHT':1}.get(light.role,2),light.id))
                factors=[1]*len(colors)
                if self.uses_audio_hits():
                    if self.audio and self.audio.running:
                        factors=self.hit_flashes.factors(self.audio.recent_hits(self.hit_flashes.cursor),now,len(colors),
                            self.settings.flash_trigger,self.settings.flash_pattern,self.settings.flash_sensitivity,self.settings.beat_every,low,
                            mode=self.settings.mode,offset=offset_seconds,period=60/(bpm or 120),accent=now<self.drop_until,roll_max_hz=self.settings.roll_max_hz)
                elif bpm and phase_at is not None:
                    if self.settings.mode=='beat_flash':
                        factors=clock_flash_factors(bpm,now,phase_at,len(colors),self.settings.flash_pattern,self.settings.beat_every,low,accent=now<self.drop_until)
                    else:
                        factors=clock_gentle_factors(bpm,now,phase_at,len(colors),self.settings.flash_pattern,self.settings.beat_every,low)
                if self.settings.mode=='beat_gentle' and now<self.drop_until:
                    factors=[low+(1-low)*math.sqrt(max(0,(factor-low)/(1-low))) for factor in factors]
                values={}
                if self.status=='starting':
                    await self.rest_music([light for light in self.lights if not light.color],steady=self.settings.white_response=='steady',force=True)
                for light,factor in zip(colors,factors):
                    offset=1 if light.role=='COLOR_RIGHT' else 0
                    color=self.palette[offset%len(self.palette)]
                    if self.settings.pulse_palette=='cycle':
                        position=(now-self.started)/(self.settings.palette_every*60/bpm) if bpm else (now-self.started)/self.settings.speed
                        color=palette_color(self.palette,position+self.palette_shift,offset)
                    xy=map_gamut(hex_to_xy(color),light.gamut)
                    brightness=self.settings.brightness*factor
                    values[light.id]=(*xy,brightness/100)
                    light.on=True;light.xy=xy;light.brightness=brightness;light.preview_color=color
                await self.stream.send(values)
                return
            if self.status=='starting' and self.white_music_active():
                await self.rest_music([light for light in self.lights if not light.color],force=True)
            for light in self.lights:
                if not light.enabled or (not light.color and self.white_music_active()): continue
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
            await self._start()

    async def _start(self):
        self.reset_audio_wait()
        self.status='starting'
        had_snapshot=self.snapshot is not None
        try:
            await self._stop_stream()
            await self.refresh()
            if not any(light.enabled for light in self.lights):
                raise ValueError('No bulbs are enabled. Select Enable bulb or Enable all four in Your lights, then click Start.')
            if self.settings.mode=='music_brightness' and not any(light.enabled and light.dimming for light in self.lights):
                raise ValueError('Enable at least one dimmable bulb for music brightness.')
            if self.uses_audio_hits() and not (self.audio and self.audio.running):
                raise ValueError('Start listening to BlackHole before using audio-triggered pulses or flashes. A BPM estimate is not required.')
            silent=self.follows_audio() and (self.audio.music_energy() or 0)<.00005
            if not silent and self.settings.mode in STREAM_MODES and not self.uses_audio_hits() and self.beat_clock()[0] is None:
                raise ValueError('Start audio detection and wait for a reliable BPM, or choose Manual BPM before starting beat effects.')
            if self.snapshot is None: self.snapshot=[]
            captured={light.id for light in self.snapshot}
            self.snapshot.extend(light.model_copy(deep=True) for light in self.lights if light.enabled and light.id not in captured)
            if silent:
                if had_snapshot:await self._restore_snapshot(clear=False)
                self.status='waiting_audio'
                self.playback_message='Waiting for music. Lights remain in their original state.'
                return
            self.started=time.monotonic()
            if self.settings.mode in STREAM_MODES:
                stream=self.stream_factory(self.client)
                await stream.start(self.lights)
                self.stream=stream
            self.hit_flashes.reset(getattr(self.audio,'hit_counter',0))
            self.drop_cursor=getattr(self.audio,'drop_counter',0);self.palette_shift=0;self.drop_until=0
            await self.apply()
        except Exception as exc:
            self.status='stopped';self.error=str(exc)
            try: await self._stop_stream()
            except Exception: pass
            raise
        self.status='running';self.error=None
        self.white_task=asyncio.create_task(self.white_music())
        self.wake.set()


    async def pause(self):
        async with self.lifecycle_lock:
            self.status='paused';self.reset_audio_wait();await self._stop_stream()

    async def stop(self):
        async with self.lifecycle_lock:
            self.status='stopped';self.reset_audio_wait();await self._stop_stream()

    async def _restore_snapshot(self, *, clear=True):
        async with self.lock:
            for light in self.snapshot or []:
                payload=command(light,light.brightness,duration=self.settings.transition,on=light.on)
                if light.color and light.xy: payload['color']={'xy':dict(zip(('x','y'),light.xy))}
                await self.client.update(light.id,payload)
            if clear:self.snapshot=None
        await self.refresh()

    async def restore(self):
        async with self.lifecycle_lock:
            self.status='stopped';self.reset_audio_wait();await self._stop_stream()
            await self._restore_snapshot()

    async def run(self):
        failures=0
        while True:
            interval=.05 if self.status=='running' and self.settings.mode in STREAM_MODES else .25 if self.follows_audio() or (self.status=='running' and self.settings.mode=='music_brightness') else 2
            try:
                await asyncio.wait_for(self.wake.wait(),timeout=min(30,interval*2**failures))
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
            try:
                await self.check_audio()
                if self.status!='running' or self.settings.mode=='static':
                    failures=0
                    continue
                await self.apply(time.monotonic()-self.started,only_if_running=True)
                self.error=None;failures=0
            except Exception as exc:
                self.error=str(exc);failures=min(4,failures+1)
                if self.stream:
                    self.status='stopped'
                    try: await self._stop_stream()
                    except Exception: pass
