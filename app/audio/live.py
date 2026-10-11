import asyncio
import threading
import time
from collections import deque
from contextlib import suppress
import numpy as np
from app.audio.bpm import detect_bpm, align_beat_phase, TempoTracker, MIN_TEMPO, MAX_TEMPO
from app.audio.onsets import HitDetector
from app.audio.percussion import PercussionDetector
from app.audio.spectrum import WINDOW_SIZE, analyze_spectrum
from app.audio.frequency import FrequencyDetector

# CoreAudio initialization and stream creation must happen on the server's
# main thread. Analysis still runs in a worker so it cannot delay Hue frames.
try:
    import sounddevice as sd
except (ImportError, OSError):
    sd = None


def input_devices():
    try:
        if sd is None: raise ValueError('sounddevice is unavailable')
        return [{'id': i, 'name': d['name'], 'channels': d['max_input_channels'],
                 'sample_rate': int(d['default_samplerate']), 'loopback': 'blackhole' in d['name'].lower()}
                for i, d in enumerate(sd.query_devices()) if d['max_input_channels'] > 0]
    except Exception as exc:
        raise ValueError('Audio input unavailable. Install the project dependencies and check audio device access.') from exc


def sample_end_time(timing,frames,sample_rate,now):
    try:
        age=float(timing.currentTime)-float(timing.inputBufferAdcTime)-frames/sample_rate
        if float(timing.inputBufferAdcTime)>0 and -.02<=age<1:return now-max(0,age)
    except (AttributeError,TypeError,ValueError):pass
    return now


class LiveBPM:
    def __init__(self):
        self.stream = None; self.task = None; self.running = False
        self.buffer = deque(); self.frames = 0; self.lock = threading.Lock()
        self.sample_rate = 48000; self.device_name = None; self.device_id = None; self.bpm = None
        self.confidence = 0; self.phase_at = None; self.last_good_at = 0
        self.error = None; self.rms = 0; self.end_time = 0
        self.pending_tempo = None; self.tempo = TempoTracker(); self.tempo_scale = 1
        self.lifecycle_lock = asyncio.Lock()
        self.hit_detector = None; self.hits = deque(maxlen=256); self.hit_counter = 0
        self.received_frames=0;self.channel_levels=[];self.analysis_channel=0
        self.callback_error=None;self.started_at=0;self.last_analysis=-float('inf')
        self.percussion_sensitivity=50;self.drum_isolation=True
        self.frequency=None;self.frequency_settings=(-15,-70,-20)
        self.percussion=None;self.drops=deque(maxlen=16);self.drop_counter=0;self.capture_latency=0;self.generation=0

    def status(self):
        with self.lock:
            last_hit = dict(self.hits[-1]) if self.hits else None
            hit_count = len(self.hits)
            percussion_hits=[hit for hit in self.hits if hit['kind']=='percussion']
            last_percussion=dict(percussion_hits[-1]) if percussion_hits else None
            age=time.monotonic()-self.end_time if self.frames else None
            frequency=self.frequency.gate.status() if self.frequency and age is not None and age<=.5 else None
        capture_state='error' if self.error and not self.running else 'idle'
        if self.running:
            if self.callback_error:capture_state='processing_error'
            elif age is None or age>2:capture_state='waiting_input' if time.monotonic()-self.started_at<2 else 'stalled'
            elif self.rms<.00005:capture_state='no_signal'
            else:capture_state='tracking' if self.current_bpm() else 'listening'
        return {'running': self.running, 'device': self.device_name, 'bpm': self.current_bpm(),
                'tempo_locked': bool(self.tempo.locked and self.current_bpm()), 'tempo_scale': self.tempo_scale,
                'device_id': self.device_id,
                'confidence': self.confidence, 'rms': self.rms, 'error': self.error,
                'warming_up': self.running and not self.error and self.frames < self.sample_rate * 6,
                'bass_level': self.hit_detector.levels['bass'] if self.hit_detector else 0,
                'drum_level': self.hit_detector.levels['drum'] if self.hit_detector else 0,
                'hit_count': hit_count, 'last_hit': last_hit,
                'percussion_hit_count':len(percussion_hits),'last_percussion':last_percussion,
                'percussion_level':self.percussion.level if self.percussion else 0,
                'percussion_pattern':self.percussion.status(time.monotonic()) if self.percussion and age is not None and age<=.5 else None,
                'last_drop':dict(self.drops[-1]) if self.drops else None,
                'drop_active':bool(self.drops and time.monotonic()-self.drops[-1]['at']<2),
                'capture_latency_ms':round(self.capture_latency*1000,1),'frequency':frequency,
                'capture_state':capture_state,'frames_received':self.received_frames,
                'channel_levels':list(self.channel_levels),'analysis_channel':self.analysis_channel,
                'input_db':round(20*np.log10(max(self.rms,1e-6)),1),'sample_rate':self.sample_rate}

    def recent_hits(self, after=0):
        with self.lock:
            if not self.running or time.monotonic()-self.end_time > .5: return []
            return [dict(hit) for hit in self.hits if hit['id'] > after]

    def recent_drops(self,after=0):
        with self.lock:
            if not self.running:return []
            return [dict(drop) for drop in self.drops if drop['id']>after and time.monotonic()-drop['at']<2]

    def music_energy(self):
        with self.lock:
            if not self.running or not self.frames or time.monotonic()-self.end_time > .5: return None
            blocks=[]; count=0
            for block in reversed(self.buffer):
                blocks.append(block); count+=len(block)
                if count>=self.sample_rate: break
        samples=np.concatenate(list(reversed(blocks)))[-self.sample_rate:]
        return float(np.sqrt(np.mean(samples**2)))

    def spectrum(self):
        # Copy only the recent FFT window. The endpoint runs this method in a
        # worker, keeping both PortAudio callbacks and light streaming free.
        with self.lock:
            running = self.running
            age = time.monotonic() - self.end_time if self.frames else None
            base = dict(running=running, ready=False, sample_rate=self.sample_rate,
                        device=self.device_name, age_ms=round(age * 1000) if age is not None else None,
                        error=self.callback_error or (self.error if not running else None),
                        percussion=self.percussion.status(time.monotonic()) if running and self.percussion and age is not None and age<=.5 else None,
                        threshold_gate=self.frequency.gate.status() if running and self.frequency and age is not None and age<=.5 else None)
            if not running:
                return dict(base, state='idle')
            if self.callback_error:
                return dict(base, state='error')
            if age is None or age > .5:
                return dict(base, state='waiting_input' if age is None else 'stalled')
            if self.frames < WINDOW_SIZE:
                return dict(base, state='warming_up')
            generation = self.generation
            sample_rate = self.sample_rate
            blocks = []; count = 0
            for block in reversed(self.buffer):
                blocks.append(block[-(WINDOW_SIZE - count):])
                count += len(blocks[-1])
                if count >= WINDOW_SIZE:
                    break
        samples = np.concatenate(list(reversed(blocks)))
        result = analyze_spectrum(samples, sample_rate)
        with self.lock:
            if not self.running or generation != self.generation:
                return dict(base, running=self.running, state='idle' if not self.running else 'warming_up')
        return dict(base, **result, ready=True, state='silent' if result['silent'] else 'live')

    def set_frequency_thresholds(self, bass, fallback, mids):
        values = (bass, fallback, mids)
        if values == self.frequency_settings: return
        detector = FrequencyDetector(self.sample_rate, *values) if self.running else None
        with self.lock:
            self.frequency_settings = values
            self.frequency = detector
            # Old crossings must not survive a threshold edit.
            self.hits = deque((hit for hit in self.hits if not hit['kind'].startswith('frequency_')), maxlen=256)

    def track_changed(self):
        # A new song must not inherit the previous song's tempo or drop history.
        with self.lock:
            self.generation+=1;self.buffer.clear();self.frames=0
            self.bpm=None;self.phase_at=None;self.confidence=0;self.last_good_at=0
            self.pending_tempo=None;self.last_analysis=-float('inf');self.tempo.reset();self.tempo_scale=1
            self.hits.clear();self.drops.clear()
            if self.running:
                self.hit_detector=HitDetector(self.sample_rate)
                self.percussion=PercussionDetector(self.sample_rate)
                self.frequency=FrequencyDetector(self.sample_rate,*self.frequency_settings)

    def current_bpm(self):
        if not (self.bpm and self.running and time.monotonic()-self.last_good_at < 6): return None
        return self.bpm*self.tempo_scale

    def scale_tempo(self, factor):
        """DJ-style ×2 / ½ correction for the current track; resets on track change."""
        scale = self.tempo_scale*factor
        if not 1/8 <= scale <= 8 or (self.bpm and not MIN_TEMPO <= self.bpm*scale <= MAX_TEMPO):
            raise ValueError(f'Tempo correction would leave the {MIN_TEMPO}–{MAX_TEMPO} BPM range')
        self.tempo_scale = 1 if abs(scale-1) < 1e-9 else scale

    async def start(self, device_id):
        async with self.lifecycle_lock:
            await self._start(device_id)

    async def _start(self, device_id):
        devices = await asyncio.to_thread(input_devices)
        device = next((d for d in devices if d['id'] == device_id), None)
        if not device:
            raise ValueError('Choose an available audio input device')
        await self._stop()
        self.generation+=1
        self.sample_rate = device['sample_rate']; self.device_name = device['name']
        self.buffer.clear(); self.frames = 0; self.bpm = None; self.phase_at = None
        self.error = None; self.pending_tempo = None; self.tempo.reset(); self.tempo_scale = 1; self.device_id = device_id
        self.received_frames=0;self.channel_levels=[];self.analysis_channel=0
        self.callback_error=None;self.started_at=time.monotonic();self.last_analysis=-float('inf')
        self.hit_detector = HitDetector(self.sample_rate)
        self.percussion=PercussionDetector(self.sample_rate)
        self.frequency=FrequencyDetector(self.sample_rate,*self.frequency_settings)
        def capture(data, frames, timing, status):
            try:
                if data.ndim!=2 or not data.shape[1] or not np.isfinite(data).all():
                    raise ValueError('Invalid audio samples')
                levels=np.sqrt(np.mean(data**2,axis=0));best=int(np.argmax(levels))
                if self.analysis_channel>=len(levels) or levels[best]>levels[self.analysis_channel]*1.5:
                    self.analysis_channel=best
                mono=data[:,self.analysis_channel].copy()
                now=time.monotonic();end=sample_end_time(timing,len(mono),self.sample_rate,now)
                self.capture_latency=max(0,now-end)
                self.channel_levels=levels.tolist()
                self.rms=max(float(levels.max()),self.rms*np.exp(-len(mono)/(self.sample_rate*.25)))
                with self.lock:
                    percussion=self.percussion;hit_detector=self.hit_detector;generation=self.generation;frequency=self.frequency
                hits=hit_detector.process(mono,end)+percussion.process(mono,end,bpm=self.current_bpm(),sensitivity=self.percussion_sensitivity,isolate=self.drum_isolation)
                hits.extend(percussion.band_hits)
                frequency_hits=frequency.process(mono,end)
                hits.extend(frequency_hits)
                hits.sort(key=lambda hit:hit['at'])
                with self.lock:
                    if generation!=self.generation:return
                    if frequency is not self.frequency:
                        hits=[hit for hit in hits if not hit['kind'].startswith('frequency_')]
                    self.buffer.append(mono);self.frames+=len(mono);self.received_frames+=len(mono);self.end_time=end
                    for hit in hits:
                        self.hit_counter+=1;self.hits.append(dict(hit,id=self.hit_counter))
                    for drop in percussion.drops:
                        self.drop_counter+=1;self.drops.append(dict(drop,id=self.drop_counter))
                    while self.hits and end-self.hits[0]['at']>3:self.hits.popleft()
                    while self.frames>self.sample_rate*20 and len(self.buffer)>1:
                        self.frames-=len(self.buffer.popleft())
                self.callback_error=None
                if status:self.error='Audio input overflow; checking the next window.'
            except Exception as exc:
                # PortAudio otherwise swallows callback exceptions and leaves a
                # stream that appears active but never provides another frame.
                self.callback_error='Audio processing failed: '+str(exc)+'. Restart listening.'
                self.error=self.callback_error
        def open_stream():
            stream = sd.InputStream(device=device_id, samplerate=self.sample_rate,
                                    channels=min(2, device['channels']), dtype='float32', latency='low', blocksize=0, callback=capture)
            try: stream.start()
            except Exception:
                stream.close(); raise
            return stream
        try:
            self.stream = open_stream()
        except Exception as exc:
            await self._stop()
            self.error='Cannot open audio input. Allow microphone/audio access for the app launching HueCanvas, then restart listening.'
            raise ValueError(self.error) from exc
        self.running = True
        self.task = asyncio.create_task(self._monitor())

    async def _monitor(self):
        while self.running:
            await asyncio.sleep(.5)
            now=time.monotonic()
            if self.callback_error:
                self.error=self.callback_error;self.bpm=None;self.confidence=0;self.phase_at=None
                continue
            with self.lock:
                if not self.frames or time.monotonic()-self.end_time > 2:
                    self.error = 'Audio input is not delivering frames. Check audio-input permissions and reconnect the selected device.'
                    continue
                samples = np.concatenate(list(self.buffer)); end = self.end_time
            rms=float(np.sqrt(np.mean(samples[-self.sample_rate:]**2)))
            if rms<.00005:
                self.bpm=None;self.confidence=0;self.phase_at=None
                self.error='Audio frames are arriving but contain silence. Play music and route its output to BlackHole using a Multi-Output Device.'
                continue
            if self.frames<self.sample_rate*6:
                self.error=None
                continue
            if now-self.last_analysis<1:continue
            self.last_analysis=now
            try:
                generation=self.generation
                result = await asyncio.to_thread(detect_bpm, samples, self.sample_rate, self.tempo.bpm)
                if generation!=self.generation:continue
                if not result:
                    self.confidence = 0
                    self.error = 'Audio is present, but no steady beat yet. Drum-triggered effects still follow the music.'
                    continue
                old = self.bpm
                self.bpm = self.tempo.update(result['bpm'])
                if self.bpm is None: continue
                phase_anchor=self.phase_at if old and abs(self.bpm-old)<=old*.04 else None
                self.confidence = result['confidence']; self.last_good_at = time.monotonic()
                # Phase follows the tracked tempo; the newest window gives the latest beat.
                self.phase_at = align_beat_phase(phase_anchor,end-result['duration']+result['last_beat_seconds'],self.bpm)
                self.error = None
            except Exception as exc:
                self.error = 'Beat analysis failed: ' + str(exc)
                self.bpm = None; self.phase_at = None

    async def stop(self):
        async with self.lifecycle_lock:
            await self._stop()

    async def _stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError): await self.task
            self.task = None
        if self.stream:
            stream = self.stream; self.stream = None
            stream.close()
        self.bpm = None; self.phase_at = None; self.confidence = 0
        self.rms = 0; self.error = None
        self.callback_error=None;self.channel_levels=[];self.received_frames=0
        with self.lock:
            self.buffer.clear(); self.frames = 0
            self.hits.clear(); self.hit_detector = None;self.percussion=None;self.frequency=None;self.drops.clear();self.capture_latency=0
