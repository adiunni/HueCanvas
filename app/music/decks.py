"""Two-deck mixer model fed by DDJ-FLX4 MIDI, used to pick the deck the crowd hears.

rekordbox exposes no deck state, so HueCanvas listens to the same controller
messages rekordbox receives: channel faders, crossfader, LOAD, PLAY and hot cue
pads. The loudest deck (fader x crossfader) is live; a challenger must stay
clearly louder for a moment before the lights follow it. rekordbox history rows
have no deck number, but rekordbox logs a track its Playback time after the
press that started it, so each row goes to the deck pressed just before then.
"""
import math
import threading
import time
from collections import deque

# DDJ-FLX4 MIDI message numbers (matching Pioneer's list and Mixxx's mapping).
FADER_MSB, FADER_LSB = 0x13, 0x33          # CC on 0xB0 (deck 1) / 0xB1 (deck 2)
CROSSFADER_STATUS = 0xB6
CROSSFADER_MSB, CROSSFADER_LSB = 0x1F, 0x3F
PLAY_NOTE = 0x0B                            # note on 0x90 / 0x91
HOTCUE_STATUS = (0x97, 0x99)                # hot cue pads 1-8 (notes 0-7), deck 1 / deck 2
LOAD_STATUS, LOAD_NOTE = 0x96, 0x46         # note 0x46 deck 1, 0x47 deck 2
# A press can start a track this long before rekordbox's Playback time runs out
# (PLAY while a track is still loading is ignored; a later press starts it).
START_WINDOW = 4.0


class Fine:
    """A 14-bit controller value sent as MSB then LSB; None until first seen."""
    def __init__(self):self.msb=None;self.lsb=0
    def set(self,number,value,msb,lsb):
        if number==msb:self.msb=value;self.lsb=0
        elif number==lsb and self.msb is not None:self.lsb=value
    @property
    def value(self):return None if self.msb is None else ((self.msb<<7)|self.lsb)/16383


class Deck:
    def __init__(self):
        self.fader=Fine();self.cued=False;self.awaiting=True
        self.starts=deque(maxlen=16);self.loaded_at=None;self.track=None;self.identified_at=None


class DeckMixer:
    def __init__(self,decks=2,*,clock=time.monotonic,hold=1.0,cut_hold=.25,margin=.05):
        self.decks=[Deck() for _ in range(decks)];self.crossfader=Fine()
        self.clock=clock;self.hold=hold;self.cut_hold=cut_hold;self.margin=margin
        self.current=None;self.challenger=None;self.challenged_at=0
        self.active=False;self.lock=threading.Lock()

    # ---- MIDI input (called from the MIDI thread).
    def handle(self,message):
        if len(message)<3:return False
        status,number,value=message[0],message[1],message[2]
        with self.lock:
            now=self.clock();deck=status&0x0F;kind=status&0xF0
            if kind==0xB0 and deck<len(self.decks) and number in (FADER_MSB,FADER_LSB):
                self.decks[deck].fader.set(number,value,FADER_MSB,FADER_LSB)
            elif status==CROSSFADER_STATUS and number in (CROSSFADER_MSB,CROSSFADER_LSB):
                self.crossfader.set(number,value,CROSSFADER_MSB,CROSSFADER_LSB)
            elif status==LOAD_STATUS and LOAD_NOTE<=number<LOAD_NOTE+len(self.decks) and value:
                self._load(number-LOAD_NOTE,now)
            elif kind==0x90 and deck<len(self.decks) and number==PLAY_NOTE and value:
                self._start(deck,now)
            elif status in HOTCUE_STATUS and number<8 and value:
                # A hot cue pad on a paused deck starts playback from that cue.
                self._start(HOTCUE_STATUS.index(status),now)
            else:return False
            self.active=True
            return True

    def _start(self,index,now):
        deck=self.decks[index];deck.cued=False;deck.starts.append(now)

    def _load(self,index,now):
        deck=self.decks[index]
        # A load onto the deck the crowd hears keeps sounding (rekordbox plays on
        # into the new track), so it also counts as a start; other loaded decks
        # are paused until PLAY, a hot cue or their history row.
        deck.cued=not (index==self.current and self._gain(index)>=.25)
        deck.awaiting=True;deck.loaded_at=now;deck.track=None
        if not deck.cued:deck.starts.append(now)

    # ---- Volume.
    def _gain(self,index):
        deck=self.decks[index]
        if deck.cued:return 0.0
        fader=deck.fader.value
        level=1.0 if fader is None else fader*fader  # rough loudness taper
        x=self.crossfader.value
        if x is None or len(self.decks)!=2:return level
        # Constant-power crossfader: deck 1 on the left, deck 2 on the right.
        return level*(math.cos(x*math.pi/2) if index==0 else math.sin(x*math.pi/2))

    def gain(self,index):
        with self.lock:return self._gain(index)

    def live(self):
        with self.lock:
            now=self.clock();gains=[self._gain(i) for i in range(len(self.decks))]
            if self.current is None:
                known=[i for i,d in enumerate(self.decks) if d.track]
                self.current=max(known or range(len(self.decks)),key=lambda i:gains[i])
                return self.current
            best=max(range(len(gains)),key=lambda i:gains[i])
            if best!=self.current and gains[best]>gains[self.current]+self.margin:
                if self.challenger!=best:self.challenger=best;self.challenged_at=now
                hold=self.cut_hold if gains[self.current]<=.01 else self.hold
                if now-self.challenged_at>=hold:self.current=best;self.challenger=None
            else:self.challenger=None
            return self.current

    # ---- Track attribution from rekordbox history.
    def assign(self,track,delay=0,at=None):
        """Attribute a history row written at clock time at (default now).

        delay is rekordbox's Playback time in seconds. Returns the deck index, or
        None when nothing shows which deck holds the track (it is then not guessed).
        """
        with self.lock:
            now=self.clock();at=now if at is None else at
            for i,deck in enumerate(self.decks):
                if deck.track and deck.track.get('id')==track.get('id'):
                    deck.cued=False  # a logged track is playing
                    return i
            expected=at-(delay or 0)
            loaded=[i for i,d in enumerate(self.decks) if d.awaiting and d.loaded_at is not None and d.loaded_at<=at]
            pressed=[]
            for i,deck in enumerate(self.decks):
                times=[t for t in deck.starts if expected-START_WINDOW<=t<=at+.5
                       and (not deck.awaiting or deck.loaded_at is None or t>=deck.loaded_at)]
                if times:pressed.append((i,min(times,key=lambda t:abs(t-expected))))
            # A deck loaded since it was last identified is the one with a new track;
            # among those, the press closest to Playback time ago started it.
            pressed_loaded=[p for p in pressed if p[0] in loaded]
            if pressed_loaded:index=min(pressed_loaded,key=lambda p:abs(p[1]-expected))[0]
            elif loaded:index=max(loaded,key=lambda i:self.decks[i].loaded_at)  # started with the mouse
            elif pressed:index=min(pressed,key=lambda p:abs(p[1]-expected))[0]  # loaded with the mouse
            elif any(d.identified_at is not None for d in self.decks):
                # Loaded and started without the controller: DJs alternate decks,
                # so the deck identified longest ago (or never) has the new track.
                index=min(range(len(self.decks)),key=lambda i:self.decks[i].identified_at if self.decks[i].identified_at is not None else float('-inf'))
            else:return None
            deck=self.decks[index]
            # A logged track is playing, however it was started.
            deck.track=dict(track);deck.awaiting=False;deck.cued=False;deck.identified_at=now
            return index

    # ---- Persistence across HueCanvas restarts.
    def export(self):
        with self.lock:
            now=self.clock()
            return [{'track':d.track,'age':None if d.identified_at is None else now-d.identified_at} for d in self.decks]

    def restore(self,saved,elapsed=0):
        """Reload export() output saved elapsed seconds ago."""
        with self.lock:
            now=self.clock()
            for deck,item in zip(self.decks,saved):
                if not isinstance(item,dict) or not item.get('track'):continue
                deck.track=dict(item['track']);deck.awaiting=False
                deck.identified_at=now-elapsed-(item.get('age') or 0)

    def track(self,index):
        with self.lock:
            track=self.decks[index].track
            return dict(track) if track else None

    def snapshot(self):
        with self.lock:
            return [{'deck':i+1,'live':i==self.current,'volume':round(self._gain(i),2),'loaded':d.cued,
                     'id':d.track.get('id') if d.track else None,
                     'track':d.track.get('track') if d.track else None,'artist':d.track.get('artist') if d.track else None,
                     'waiting':d.awaiting and d.loaded_at is not None} for i,d in enumerate(self.decks)]
