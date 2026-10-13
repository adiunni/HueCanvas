import asyncio
import hashlib
import time
from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from starlette.concurrency import run_in_threadpool
from app import config
from app.events.analysis import analyze_wav, MAX_BYTES
from app.events.effects import envelope
from app.events.models import ALGORITHM_VERSION, Cue, EventOptions, TransportUpdate
from app.events.store import CueStore
from app.events.live import LiveEvents, live_measurements


def store():
    return CueStore(config.DATA_DIR / 'events')


def refresh_cues(engine):
    engine.events.cues = store().cues(engine.events.clock.transport.track_id)


def create_router(get_engine):
    router = APIRouter(prefix='/api/events')
    analysis_lock = asyncio.Lock()

    @router.get('')
    async def state(track_id: str | None = Query(None, max_length=300)):
        engine = get_engine()
        track_id = track_id or engine.events.clock.transport.track_id
        saved = store().read(track_id)
        # Feature arrays remain in the track cache, not every dashboard poll.
        analysis = saved['analysis']
        summary = {k: v for k, v in analysis.items() if k not in ('features', 'normalized', 'onset_indices', 'beat_candidates', 'downbeat_candidates')} if analysis else None
        return {**engine.events.status(), 'track_id': track_id, 'events': saved['events'], 'analysis': summary}

    @router.put('/options')
    async def options(body: EventOptions):
        engine = get_engine()
        old = engine.events.options
        was_running = engine.status == 'running'
        engine.events.options = body
        if not body.enabled:
            engine.events.clear()
        if old.live_enabled != body.live_enabled:
            engine.events.live_candidates.clear()
            engine.events.last_live.clear()
        if was_running and (old.enabled, old.output) != (body.enabled, body.output):
            await engine.start()
        engine.wake.set()
        return engine.events.status()

    @router.put('/cue')
    async def put(body: Cue):
        saved = store().put(body)
        engine = get_engine()
        refresh_cues(engine)
        if engine.events.active and engine.events.active[0].id == saved.id:
            engine.events.clear()
        return saved.model_dump(mode='json')

    @router.post('/analyze')
    async def analyze(track_id: str = Query(min_length=1, max_length=300), file: UploadFile = File(...)):
        if analysis_lock.locked():
            raise HTTPException(409, 'Another track is being analyzed')
        async with analysis_lock:
            try:
                data = await file.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise HTTPException(413, 'WAV must be at most 128 MB')
                saved = store().read(track_id)
                analysis = saved['analysis']
                digest = hashlib.sha256(data).hexdigest()
                if analysis and analysis.get('audio_sha256') == digest and analysis.get('algorithm_version') == ALGORITHM_VERSION:
                    return {'cached': True, 'count': len(saved['events'])}
                candidates, analysis = await run_in_threadpool(analyze_wav, data, track_id)
                await run_in_threadpool(store().cache_features,track_id,analysis)
                # Re-read after analysis: edits made during the worker must survive.
                saved = store().merge_analysis(track_id, candidates, analysis)
                refresh_cues(get_engine())
                return {'cached': False, 'count': len(saved['events'])}
            finally:
                await file.close()

    @router.put('/transport')
    async def transport(body: TransportUpdate):
        engine = get_engine()
        engine.events.options.follow_music = False
        engine.events.follow_paused = False
        engine.events.transport(body, time.monotonic())
        refresh_cues(engine)
        engine.wake.set()
        return engine.events.status()

    @router.post('/preview')
    async def preview(body: Cue):
        engine = get_engine()
        pre=body.envelope.pre_dim
        duration = pre + max(1,body.envelope.attack) + max(2, body.envelope.hold) + max(4, body.envelope.decay)
        return {'palette': engine.palette, 'duration': duration, 'samples': [
            {'at': i * duration / 100-pre, **envelope(body, i * duration / 100-pre,
                                               reduced=engine.events.options.reduced_motion,
                                               ambient=engine.events.options.ambient_level),
             'white_level':envelope(body,i*duration/100-pre,slow=True,reduced=engine.events.options.reduced_motion,
                                    ambient=engine.events.options.ambient_level)['level']} for i in range(101)]}

    @router.post('/audition')
    async def audition(body: Cue):
        engine = get_engine()
        if not engine.events.options.enabled or engine.status != 'running':
            raise ValueError('Enable event effects and press Start before auditioning lights')
        engine.events.resume(time.monotonic())
        engine.events.trigger(body, time.monotonic(), age=-body.envelope.pre_dim,preview=True)
        engine.wake.set()
        return {'ok': True}

    return router


async def live_worker(engine, audio):
    detector = LiveEvents()
    generation = None
    identity = None
    epoch = None
    while True:
        await asyncio.sleep(.25)
        runtime = engine.events
        if not runtime.options.live_enabled or not getattr(audio, 'running', False) or not hasattr(audio, 'lock'):
            detector = LiveEvents(); generation = None
            continue
        try:
            began = time.monotonic()
            data = await asyncio.to_thread(live_measurements, audio)
            if data is None or began - data[0] > .5:
                continue
            at, current, rms, bass, flux, density = data
            track_id = runtime.clock.transport.track_id
            if (generation, identity, epoch) != (current, track_id, runtime.clock.epoch):
                detector = LiveEvents(); generation = current; identity = track_id; epoch = runtime.clock.epoch
            if current != audio.generation:
                continue
            now = time.monotonic()
            candidates = detector.feed(at, rms, bass, flux, density, now=now, track_id=track_id,
                                       position=runtime.clock.position(at), threshold=runtime.options.confidence)
            runtime.processing_ms = round((time.monotonic() - began) * 1000, 2)
            runtime.error = None
            for cue in candidates:
                runtime.live(cue, now, *engine.beat_clock(),can_trigger=engine.status=='running')
            engine.wake.set()
        except Exception as exc:
            runtime.error = 'Live event analysis failed: ' + str(exc)
