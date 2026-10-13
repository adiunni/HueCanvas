import asyncio
import pytest
from app.events.models import Cue, EventOptions, Envelope, TransportUpdate
from app.events.store import CueStore
from app.events.timing import CueClock
from app.events.effects import envelope
from app.animation.engine import Engine
from app.hue.mock import MockClient


def final(position=5, **kwargs):
    return Cue(track_id='test', type='FINAL_IMPACT', position=position, **kwargs)


def test_manual_final_impact_drives_four_bulbs_and_preserves_palette(monkeypatch):
    import app.animation.engine as module
    now = [100.]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])

    async def scenario():
        client = MockClient()
        engine = Engine(client)
        engine.events.options = EventOptions(enabled=True, output='entertainment', reduced_motion=False)
        engine.events.cues = [final()]
        engine.events.transport(TransportUpdate(track_id='test', position=0), now[0])
        palette = list(engine.palette)
        await engine.start()
        assert engine.stream and engine.white_task
        frames = len(engine.stream.frames)
        now[0] = 105.3
        await engine.apply()
        now[0] = 107.3
        await engine.rest_music(engine.lights[2:])
        assert len(engine.events.history) == 1
        assert engine.lights[0].brightness > 39
        assert engine.lights[2].brightness > 16.25
        assert len(engine.stream.frames) > frames
        assert engine.palette == palette
        assert all('color' not in p and p['dynamics']['duration'] >= 1000 for _, p in client.commands)
        assert all(id in ('2', '3') for id, _ in client.commands)
        count = len(client.commands)
        now[0] += .2
        await engine.apply()
        await engine.rest_music(engine.lights[2:])
        assert len(client.commands) == count
        assert len(engine.events.history) == 1
        await engine.restore()
        assert engine.stream is None and engine.white_task is None
        assert all(not light.on for light in client.lights)

    asyncio.run(scenario())


def test_rest_event_updates_keep_two_second_spacing(monkeypatch):
    import app.animation.engine as module
    now = [100.]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])

    async def scenario():
        engine = Engine(MockClient())
        engine.events.options = EventOptions(enabled=True)
        engine.events.cues = [final()]
        engine.events.transport(TransportUpdate(track_id='test', position=0), now[0])
        await engine.start()
        writes = []
        engine.client.commands.clear()
        for i in range(1, 241):
            now[0] = 100 + i * .05
            await engine.apply()
            await engine.rest_music(engine.lights[2:], steady=True)
            writes.extend((id, now[0], p) for id, p in engine.client.commands)
            engine.client.commands.clear()
        for light in engine.lights:
            times = [at for id, at, _ in writes if id == light.id]
            assert all(b - a >= 2 - 1e-8 for a, b in zip(times, times[1:]))
        assert all(p['dynamics']['duration'] >= 1000 for _, _, p in writes)
        assert all('color' not in p for id, _, p in writes if id in ('2', '3'))
        await engine.stop()

    asyncio.run(scenario())


def test_clock_pause_seek_rate_loop_and_deduplication():
    clock = CueClock()
    cue = final()
    clock.update(TransportUpdate(track_id='test', position=0, playing=True), 100)
    assert not clock.poll([cue], 104)
    clock.update(TransportUpdate(track_id='test', position=4, playing=False), 104)
    assert not clock.poll([cue], 200)
    clock.update(TransportUpdate(track_id='test', position=4, playing=True, rate=2), 200)
    assert len(clock.poll([cue], 200.5)) == 1
    assert not clock.poll([cue], 200.6)
    clock.update(TransportUpdate(track_id='test', position=4, playing=True, seek=True), 201)
    assert len(clock.poll([cue], 202)) == 1
    clock.update(TransportUpdate(track_id='test', position=4, playing=True, loop_start=4, loop_end=6, seek=True), 203)
    assert len(clock.poll([cue], 204)) == 1
    assert not clock.poll([cue], 205)
    assert len(clock.poll([cue], 206)) == 1
    clock.update(TransportUpdate(track_id='different', position=0, playing=True), 207)
    assert not clock.poll([cue], 212)


def test_reanalysis_preserves_moved_disabled_rejected_and_manual_cues(tmp_path):
    store = CueStore(tmp_path)
    old = final(source='offline', review='pending')
    store.merge_analysis('test', [old], {'version': 'old'})
    old.position = 7
    old.enabled = False
    old.review = 'rejected'
    store.put(old)
    store.put(final(10))
    store.merge_analysis('test', [final(5.1, source='offline'), final(20, source='offline')], {'version': 'new'})
    cues = store.cues('test')
    assert [c.position for c in cues] == [7, 10, 20]
    assert cues[0].original_position == 5 and not cues[0].enabled and cues[0].review == 'rejected'
    assert CueStore(tmp_path).cues('test') == cues


def test_envelopes_are_deterministic_bounded_and_reduced_motion_has_no_motion():
    for preset in ['CINEMATIC_IMPACT', 'DROP_RELEASE', 'BUILD_RAMP', 'FINAL_RESOLUTION', 'OUTRO_FADE']:
        cue = final(effect=preset, envelope=Envelope(pre_dim=1))
        for age in [-1, -.5, 0, .25, 1, 3, 8, 40]:
            value = envelope(cue, age, reduced=True)
            assert value == envelope(cue, age, reduced=True)
            assert 0 <= value['level'] <= 1 and value['motion'] == 0


def test_invalid_nonfinite_times_and_loop_are_rejected():
    for value in [float('nan'), float('inf'), -1]:
        with pytest.raises(ValueError):
            final(value)
    with pytest.raises(ValueError):
        TransportUpdate(track_id='x', position=0, loop_start=5, loop_end=4)


def test_loop_wrap_keeps_last_cue_and_rearms_first_without_triggering_outside_loop():
    clock=CueClock()
    first=final(4,envelope=Envelope(pre_dim=1))
    last=final(5.98)
    outside=final(6.2,envelope=Envelope(pre_dim=1))
    clock.update(TransportUpdate(track_id='test',position=4,playing=True,loop_start=4,loop_end=6),100)
    clock.poll([first,last,outside],101.9)
    due=clock.poll([first,last,outside],102.02)
    assert [c.id for c,age in due]==[last.id,first.id]
    assert due[0][1]==pytest.approx(.04)
    assert not clock.poll([first,last,outside],102.05)


def test_known_latency_and_predim_do_not_shift_cue_timestamps():
    clock=CueClock();cue=final(5,envelope=Envelope(pre_dim=1))
    clock.update(TransportUpdate(track_id='test',position=0,playing=True,rate=2),100)
    assert not clock.poll([cue],101.39,advance=.1)
    due=clock.poll([cue],101.4,advance=.1)
    assert len(due)==1 and due[0][1]==pytest.approx(-1)
    assert cue.position==5
    clock.update(TransportUpdate(track_id='test',position=4.5,playing=True,seek=True),200)
    joined=clock.poll([cue],200.05)
    assert len(joined)==1 and joined[0][1]==pytest.approx(-.45)
    clock.update(TransportUpdate(track_id='test',position=6,playing=True,seek=True),201)
    assert not clock.poll([cue],201.05)


def test_effect_freezes_on_pause_and_resume_keeps_elapsed_time():
    from app.events.runtime import EventRuntime
    runtime=EventRuntime();runtime.options.enabled=True
    runtime.transport(TransportUpdate(track_id='test',position=0,playing=True),100)
    runtime.trigger(final(),100)
    before=runtime.sample(101)
    runtime.transport(TransportUpdate(track_id='test',position=1,playing=False),101)
    assert runtime.sample(200)==before
    runtime.transport(TransportUpdate(track_id='test',position=1,playing=True),200)
    assert runtime.sample(200)==before
    assert runtime.sample(203)!=before


def test_predim_joins_attack_continuously_and_build_reaches_its_peak():
    cue=final(envelope=Envelope(pre_dim=1))
    assert envelope(cue,-1e-6)['level']==pytest.approx(envelope(cue,0)['level'],abs=1e-5)
    build=final(effect='BUILD_RAMP')
    assert envelope(build,2)['level']>envelope(build,1)['level']
    assert not envelope(build,6)['done'] and envelope(build,7)['done']


def test_event_stream_caps_frames_even_when_api_or_worker_wakes_early(monkeypatch):
    import app.animation.engine as module
    now=[100.];monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    async def scenario():
        engine=Engine(MockClient());engine.events.options=EventOptions(enabled=True,output='entertainment')
        await engine.start()
        for i in range(1,101):
            now[0]=100+i*.01
            await engine.apply()
        assert len(engine.stream.frames)==21
        await engine.stop()
    asyncio.run(scenario())
