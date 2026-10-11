import asyncio
import sqlite3
import pytest
from app.music.controller import ControllerInput, POSITION_QUERY
from app.music.decks import DeckMixer
from app.music.rekordbox import Rekordbox
from tests.test_rekordbox import add, library  # noqa: F401  (fixture)


class Clock:
    now=100.0
    def __call__(self):return self.now


def fader(deck,value):
    # 14-bit fader: MSB then LSB, as the FLX4 sends it.
    raw=round(value*16383);return [[0xB0+deck,0x13,raw>>7],[0xB0+deck,0x33,raw&0x7F]]


def crossfader(value):
    raw=round(value*16383);return [[0xB6,0x1F,raw>>7],[0xB6,0x3F,raw&0x7F]]


def load(deck):return [[0x96,0x46+deck,0x7F],[0x96,0x46+deck,0x00]]
def play(deck):return [[0x90+deck,0x0B,0x7F],[0x90+deck,0x0B,0x00]]
def hotcue(deck,pad):return [[(0x97,0x99)[deck],pad,0x7F],[(0x97,0x99)[deck],pad,0x00]]


@pytest.fixture(autouse=True)
def no_machine_settings(monkeypatch):
    # Keep tests independent of this Mac's rekordbox Playback time.
    import app.music.rekordbox as module
    monkeypatch.setattr(module,'played_time',lambda path=None:None)


def send(mixer,*groups):
    for group in groups:
        for message in group:mixer.handle(message)


def track(name):return {'available':True,'id':f'rekordbox:{name}','track':name,'artist':'A','album':''}


def mixer_with_two_tracks(clock):
    mixer=DeckMixer(clock=clock)
    send(mixer,fader(0,1),fader(1,1),crossfader(0))
    assert mixer.live()==0
    send(mixer,load(0),play(0))  # LOAD on the live deck keeps it playing
    assert mixer.assign(track('one'))==0
    send(mixer,load(1),play(1))
    assert mixer.assign(track('two'))==1
    return mixer


def test_fourteen_bit_faders_and_constant_power_crossfader():
    mixer=DeckMixer()
    send(mixer,fader(0,.5))
    assert mixer.decks[0].fader.value==pytest.approx(.5,abs=1e-3)
    send(mixer,crossfader(.5))
    # Fader taper is squared; centre crossfader is -3 dB for both decks.
    assert mixer.gain(0)==pytest.approx(.25*.7071,abs=2e-3)
    assert mixer.gain(1)==pytest.approx(.7071,abs=2e-3)  # untouched fader counts as up
    assert mixer.active


def test_crossfade_switches_live_deck_only_after_a_sustained_margin():
    clock=Clock();mixer=mixer_with_two_tracks(clock)
    send(mixer,crossfader(.5));clock.now+=5
    assert mixer.live()==0  # equal volume keeps the current deck
    send(mixer,crossfader(.6));assert mixer.live()==0
    clock.now+=.5;assert mixer.live()==0
    send(mixer,crossfader(.4));clock.now+=1;assert mixer.live()==0  # wobble back resets the hold
    send(mixer,crossfader(.65));mixer.live();clock.now+=1.01
    assert mixer.live()==1 and mixer.track(1)['track']=='two'


def test_channel_fader_cut_switches_quickly():
    clock=Clock();mixer=mixer_with_two_tracks(clock)
    send(mixer,crossfader(.5),fader(0,0))
    mixer.live();clock.now+=.3
    assert mixer.live()==1


def test_freshly_loaded_deck_is_silent_until_play():
    clock=Clock();mixer=mixer_with_two_tracks(clock)
    send(mixer,fader(0,0),load(0))
    assert mixer.gain(0)==0 and mixer.decks[0].awaiting
    send(mixer,fader(0,1));assert mixer.gain(0)==0
    send(mixer,play(0));assert mixer.gain(0)>0


def test_load_on_the_live_audible_deck_keeps_it_audible_and_waits_for_history():
    clock=Clock();mixer=mixer_with_two_tracks(clock)
    send(mixer,load(0))
    assert mixer.live()==0 and not mixer.decks[0].cued and mixer.gain(0)>0
    assert mixer.decks[0].awaiting and mixer.track(0) is None
    assert mixer.assign(track('three'))==0


def test_hot_cue_pad_starts_a_loaded_deck():
    clock=Clock();mixer=mixer_with_two_tracks(clock)
    send(mixer,fader(0,1),crossfader(1));mixer.live();clock.now+=.3;mixer.live()
    send(mixer,load(0));assert mixer.gain(0)==0
    send(mixer,crossfader(0),hotcue(0,7))
    assert mixer.gain(0)>0 and mixer.decks[0].starts[-1]==clock.now


def test_history_row_unmutes_a_deck_started_without_a_known_button():
    clock=Clock();mixer=mixer_with_two_tracks(clock)
    send(mixer,crossfader(1));mixer.live();clock.now+=.3;mixer.live()
    send(mixer,load(0));assert mixer.gain(0)==0
    assert mixer.assign(track('three'))==0 and not mixer.decks[0].cued


def test_history_rows_are_attributed_to_the_loaded_deck_not_the_live_one():
    clock=Clock();mixer=mixer_with_two_tracks(clock)
    send(mixer,crossfader(1));mixer.live();clock.now+=.3;assert mixer.live()==1
    clock.now+=60;send(mixer,load(0),play(0))  # next track cued on deck 1 while deck 2 is live
    assert mixer.decks[0].awaiting
    assert mixer.assign(track('three'))==0
    assert mixer.assign(track('two'))==1  # repeated row for a known track keeps its deck


def test_row_goes_to_deck_started_closest_to_playback_time_ago():
    clock=Clock();mixer=DeckMixer(clock=clock)
    send(mixer,load(0),load(1),play(1));clock.now+=50;send(mixer,play(0));clock.now+=10
    # Playback time 60 s: deck 2 started 60 s ago, deck 1 only 10 s ago.
    assert mixer.assign(track('first logged'),delay=60)==1
    clock=Clock();mixer=DeckMixer(clock=clock)
    send(mixer,load(0),load(1),play(1));clock.now+=50;send(mixer,play(0));clock.now+=1
    # Playback time 1 s: the deck started a second ago is the one rekordbox logged.
    assert mixer.assign(track('first logged'),delay=1)==0


def test_recorded_flx4_session_attributes_both_decks():
    # Replays the 14:38 monitor log: LOAD deck 1 (logged 2 s later without PLAY),
    # LOAD deck 2, hot cue pads on both decks, Tremor logged a second later.
    clock=Clock();mixer=DeckMixer(clock=clock)
    send(mixer,load(0));clock.now+=1.8
    assert mixer.assign(track('High on Life'),delay=1)==0
    clock.now+=10;send(mixer,load(1));clock.now+=11
    send(mixer,hotcue(1,1),hotcue(0,7));clock.now+=1
    assert mixer.assign(track('Tremor'),delay=1)==1
    assert mixer.track(0)['track']=='High on Life' and mixer.gain(1)>0


def replay(events,rows,delay=1,poll=.4):
    """events: (time, deck, kind); rows: (time, title). Rows are seen poll seconds after rekordbox writes them."""
    clock=Clock();mixer=DeckMixer(clock=clock);start=clock.now;found={}
    timeline=[(t,0,e) for t,*e in events]+[(t+poll,1,(title,t)) for t,title in rows]
    for t,kind,item in sorted(timeline):
        clock.now=start+t
        if kind==0:
            deck,action=item;send(mixer,{'load':load,'play':play,'hotcue':lambda d:hotcue(d,0)}[action](deck))
        else:
            title,written=item;found[title]=mixer.assign(track(title),delay=delay,at=start+written)
    return found


def test_recorded_15_02_session_ignored_play_then_hot_cue():
    # FLX4 presses and history rows from the 15:02 monitor (seconds after 15:02:00).
    events=[(21,0,'load'),(24,0,'play'),(27,0,'play'),(29,1,'load'),(31,1,'play'),
            (44,0,'load'),(45,0,'play'),(53,0,'hotcue')]  # the PLAY at :45 came while still loading
    rows=[(25.13,"Don't You Worry Child"),(31.85,'Birds Of A Feather'),(54.39,'Without You')]
    assert replay(events,rows)=={"Don't You Worry Child":0,'Birds Of A Feather':1,'Without You':0}


def test_recorded_15_07_session():
    events=[(17,0,'load'),(21,1,'load'),(24,0,'play'),(29,1,'play'),
            (37,1,'hotcue'),(38,0,'hotcue'),(40,1,'hotcue'),(40,0,'hotcue'),(58,0,'play')]
    rows=[(24.89,'High on Life'),(30.49,'Tremor')]
    assert replay(events,rows)=={'High on Life':0,'Tremor':1}


def test_rows_without_any_evidence_are_not_guessed():
    clock=Clock();mixer=DeckMixer(clock=clock)
    send(mixer,fader(0,1),crossfader(.3))
    assert mixer.assign(track('logged before HueCanvas knew the decks'))is None
    assert mixer.track(0) is None and mixer.track(1) is None


def test_decks_survive_export_and_restore():
    clock=Clock();mixer=mixer_with_two_tracks(clock)
    saved=mixer.export()
    fresh=DeckMixer(clock=clock);fresh.restore(saved,elapsed=30)
    assert fresh.track(0)['track']=='one' and fresh.track(1)['track']=='two'
    assert not fresh.decks[0].awaiting
    clock.now+=10;assert fresh.assign(track('three'))==0  # deck identified longest ago


def test_mouse_loads_alternate_to_the_deck_identified_longest_ago():
    clock=Clock();mixer=mixer_with_two_tracks(clock)
    clock.now+=10;assert mixer.assign(track('three'))==0
    clock.now+=10;assert mixer.assign(track('four'))==1


def test_unrelated_midi_is_ignored():
    mixer=DeckMixer()
    assert not mixer.handle([0xB0,0x07,10])  # EQ high
    assert not mixer.handle([0xF0,0x00])
    assert not mixer.active


# ---- Controller input.
class FakePort:
    def __init__(self,ports):self.ports=ports;self.opened=None;self.callback=None;self.sent=[]
    def get_ports(self):return self.ports
    def ignore_types(self,**kwargs):pass
    def open_port(self,index):self.opened=index
    def set_callback(self,callback):self.callback=callback
    def cancel_callback(self):self.callback=None
    def close_port(self):self.opened=None
    def send_message(self,message):self.sent.append(message)


class FakeMidi:
    def __init__(self):self.ports=['IAC Bus 1','DDJ-FLX4'];self.inputs=[];self.outputs=[]
    def MidiIn(self):port=FakePort(self.ports);self.inputs.append(port);return port
    def MidiOut(self):port=FakePort(self.ports);self.outputs.append(port);return port


def test_controller_connects_queries_positions_and_reconnects():
    clock=Clock();midi=FakeMidi();mixer=DeckMixer()
    controller=ControllerInput(mixer,'DDJ-FLX4',clock=clock,midi=midi)
    controller.poll()
    port=next(p for p in midi.inputs if p.opened is not None)
    assert controller.connected and port.opened==1
    assert midi.outputs[-1].sent==[POSITION_QUERY]
    port.callback(([0xB1,0x13,64],0.0))
    assert mixer.active and mixer.decks[1].fader.msb==64
    midi.ports=['IAC Bus 1'];controller.poll()
    assert controller.connected  # throttled
    clock.now+=5;controller.poll()
    assert not controller.connected and 'not connected' in controller.reason
    midi.ports=['DDJ-FLX4'];clock.now+=5;controller.poll()
    assert controller.connected


def test_controller_reports_missing_midi_support(monkeypatch):
    import builtins
    real=builtins.__import__
    def blocked(name,*args,**kwargs):
        if name=='rtmidi':raise ImportError(name)
        return real(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',blocked)
    controller=ControllerInput(DeckMixer())
    controller.poll()
    assert not controller.connected and 'Install' in controller.reason


# ---- Provider with decks.
def test_provider_reports_live_deck_and_switches_on_crossfade(library):  # noqa: F811
    clock=Clock();mixer=DeckMixer(clock=clock)
    rb=Rekordbox(library/'master.db',connect=lambda:sqlite3.connect(library/'master.db',check_same_thread=False),mixer=mixer)
    async def scenario():
        send(mixer,fader(0,1),fader(1,1),crossfader(0));mixer.live()
        send(mixer,load(0),play(0))
        add(library,'1','Opening track')
        assert (await rb.current_track())['track']=='Opening track'
        send(mixer,load(1),play(1))
        add(library,'2','Incoming track')  # logged while still cued in headphones
        current=await rb.current_track()
        assert current['track']=='Opening track' and current['deck']==1
        send(mixer,crossfader(1));await rb.current_track();clock.now+=.3
        current=await rb.current_track()
        assert current['track']=='Incoming track' and current['deck']==2
        send(mixer,load(0),play(0),crossfader(.2))  # new deck-1 track not yet logged
        await rb.current_track();clock.now+=2
        assert (await rb.current_track())['track']=='Incoming track'  # keeps last known track
        assert rb.decks()['decks'][0]['waiting']
    asyncio.run(scenario())


def test_provider_without_controller_activity_follows_newest_history(library):  # noqa: F811
    rb=Rekordbox(library/'master.db',connect=lambda:sqlite3.connect(library/'master.db',check_same_thread=False),mixer=DeckMixer())
    async def scenario():
        add(library,'1','First',ago=30);assert (await rb.current_track())['track']=='First'
        add(library,'2','Second');assert (await rb.current_track())['track']=='Second'
    asyncio.run(scenario())


def test_provider_restores_decks_only_for_the_same_rekordbox_run(library,tmp_path):  # noqa: F811
    state=tmp_path/'decks.json';running=['42:Sun Oct 11 13:41:00 2026']
    def provider():
        mixer=DeckMixer(clock=Clock())
        return Rekordbox(library/'master.db',connect=lambda:sqlite3.connect(library/'master.db',check_same_thread=False),
                         mixer=mixer,state_path=state,instance=lambda:running[0]),mixer
    async def scenario():
        rb,mixer=provider()
        send(mixer,fader(0,1),fader(1,1),crossfader(0));mixer.live();send(mixer,load(0),play(0))
        add(library,'1','Opening track');await rb.current_track()
        assert state.exists() and oct(state.stat().st_mode&0o777)=='0o600'
        rb,mixer=provider();await rb.current_track()  # HueCanvas restarted, rekordbox still running
        assert mixer.track(0)['track']=='Opening track'
        running[0]='77:Sun Oct 11 16:00:00 2026'
        rb,mixer=provider();await rb.current_track()  # rekordbox was restarted: decks are empty again
        assert mixer.track(0) is None
    asyncio.run(scenario())
