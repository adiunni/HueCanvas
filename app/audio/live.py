import asyncio
import threading
import time
from collections import deque
from contextlib import suppress
import numpy as np
from app.audio.bpm import detect_bpm, align_beat_phase
from app.audio.onsets import HitDetector

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


class LiveBPM:
    def __init__(self):
        self.stream = None; self.task = None; self.running = False
        self.buffer = deque(); self.frames = 0; self.lock = threading.Lock()
        self.sample_rate = 48000; self.device_name = None; self.bpm = None
        self.confidence = 0; self.phase_at = None; self.last_good_at = 0
        self.error = None; self.rms = 0; self.end_time = 0
        self.pending_tempo = None
        self.lifecycle_lock = asyncio.Lock()
        self.hit_detector = None; self.hits = deque(maxlen=256); self.hit_counter = 0
        self.received_frames=0;self.channel_levels=[];self.analysis_channel=0
        self.callback_error=None;self.started_at=0;self.last_analysis=-float('inf')

    def status(self):
        with self.lock:
            last_hit = dict(self.hits[-1]) if self.hits else None
            hit_count = len(self.hits)
            age=time.monotonic()-self.end_time if self.frames else None
        capture_state='idle'
        if self.running:
            if self.callback_error:capture_state='processing_error'
            elif age is None or age>2:capture_state='waiting_input' if time.monotonic()-self.started_at<2 else 'stalled'
            elif self.rms<.00005:capture_state='no_signal'
            else:capture_state='tracking' if self.current_bpm() else 'listening'
        return {'running': self.running, 'device': self.device_name, 'bpm': self.current_bpm(),
                'confidence': self.confidence, 'rms': self.rms, 'error': self.error,
                'warming_up': self.running and not self.error and self.frames < self.sample_rate * 8,
                'bass_level': self.hit_detector.levels['bass'] if self.hit_detector else 0,
                'drum_level': self.hit_detector.levels['drum'] if self.hit_detector else 0,
                'hit_count': hit_count, 'last_hit': last_hit,
                'capture_state':capture_state,'frames_received':self.received_frames,
                'channel_levels':list(self.channel_levels),'analysis_channel':self.analysis_channel,
                'input_db':round(20*np.log10(max(self.rms,1e-6)),1),'sample_rate':self.sample_rate}

    def recent_hits(self, after=0):
        with self.lock:
            if not self.running or time.monotonic()-self.end_time > .5: return []
            return [dict(hit) for hit in self.hits if hit['id'] > after]

    def music_energy(self):
        with self.lock:
            if not self.running or not self.frames or time.monotonic()-self.end_time > .5: return None
            blocks=[]; count=0
            for block in reversed(self.buffer):
                blocks.append(block); count+=len(block)
                if count>=self.sample_rate: break
        samples=np.concatenate(list(reversed(blocks)))[-self.sample_rate:]
        return float(np.sqrt(np.mean(samples**2)))

    def current_bpm(self):
        return self.bpm if self.running and time.monotonic()-self.last_good_at < 6 else None

    async def start(self, device_id):
        async with self.lifecycle_lock:
            await self._start(device_id)

    async def _start(self, device_id):
        devices = await asyncio.to_thread(input_devices)
        device = next((d for d in devices if d['id'] == device_id), None)
        if not device:
            raise ValueError('Choose an available audio input device')
        await self._stop()
        self.sample_rate = device['sample_rate']; self.device_name = device['name']
        self.buffer.clear(); self.frames = 0; self.bpm = None; self.phase_at = None
        self.error = None; self.pending_tempo = None
        self.received_frames=0;self.channel_levels=[];self.analysis_channel=0
        self.callback_error=None;self.started_at=time.monotonic();self.last_analysis=-float('inf')
        self.hit_detector = HitDetector(self.sample_rate)
        def capture(data, frames, timing, status):
            try:
                if data.ndim!=2 or not data.shape[1] or not np.isfinite(data).all():
                    raise ValueError('Invalid audio samples')
                levels=np.sqrt(np.mean(data**2,axis=0));best=int(np.argmax(levels))
                if self.analysis_channel>=len(levels) or levels[best]>levels[self.analysis_channel]*1.5:
                    self.analysis_channel=best
                mono=data[:,self.analysis_channel].copy()
                end=time.monotonic()
                self.channel_levels=levels.tolist()
                self.rms=max(float(levels.max()),self.rms*np.exp(-len(mono)/(self.sample_rate*.25)))
                hits=self.hit_detector.process(mono,end)
                with self.lock:
                    self.buffer.append(mono);self.frames+=len(mono);self.received_frames+=len(mono);self.end_time=end
                    for hit in hits:
                        self.hit_counter+=1;self.hits.append(dict(hit,id=self.hit_counter))
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
                                    channels=min(2, device['channels']), dtype='float32', callback=capture)
            try: stream.start()
            except Exception:
                stream.close(); raise
            return stream
        try:
            self.stream = open_stream()
        except Exception as exc:
            raise ValueError('Cannot open audio input. Allow microphone/audio access for the app launching HueCanvas.') from exc
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
            if self.frames<self.sample_rate*8:
                self.error=None
                continue
            if now-self.last_analysis<2:continue
            self.last_analysis=now
            try:
                result = await asyncio.to_thread(detect_bpm, samples, self.sample_rate, self.bpm)
                if not result:
                    self.bpm = None; self.confidence = 0; self.phase_at = None
                    self.error = 'Audio is present, but no stable BPM is detected yet. Bass/drum triggers and music brightness can still follow the audio; try a rhythmic section or Manual BPM.'
                    continue
                new = result['bpm']
                if self.bpm and abs(new-self.bpm) > self.bpm*.08:
                    if self.pending_tempo is None or abs(new-self.pending_tempo)>3:
                        self.pending_tempo = new
                        continue
                self.pending_tempo = None
                phase_anchor=self.phase_at if self.bpm and abs(new-self.bpm)<=self.bpm*.08 else None
                self.bpm = new if not self.bpm or abs(new-self.bpm)>self.bpm*.08 else .65*self.bpm+.35*new
                self.confidence = result['confidence']; self.last_good_at = time.monotonic()
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
            self.hits.clear(); self.hit_detector = None
