"""Follow rekordbox through its play history in master.db, opened read-only.

rekordbox has no scripting interface. It appends a history row once a deck has
played a track for its Playback time preference (Preferences > Advanced >
Browse). History has no deck number; with a DJ controller connected, a DeckMixer
attributes each row to the deck pressed just before it and the louder deck
decides the current track. Deck assignments are saved, so restarting HueCanvas
while rekordbox keeps running does not lose them. Without a controller, the
newest history row is the current track.
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


ROWS='''select c.ID,c.Title,coalesce(a.Name,''),coalesce(al.Name,''),s.created_at
from djmdSongHistory s join djmdContent c on c.ID=s.ContentID
left join djmdArtist a on a.ID=c.ArtistID left join djmdAlbum al on al.ID=c.AlbumID
where s.rb_local_deleted=0'''
LATEST=ROWS+' order by s.created_at desc limit 1'
SINCE=ROWS+' and s.created_at>? order by s.created_at limit 50'


def default_database():
    if configured:=os.getenv('REKORDBOX_DB'):return Path(configured).expanduser()
    if sys.platform=='win32':return Path(os.getenv('APPDATA',''))/'Pioneer'/'rekordbox'/'master.db'
    return Path.home()/'Library'/'Pioneer'/'rekordbox'/'master.db'


def settings_file():
    if sys.platform=='win32':return Path(os.getenv('APPDATA',''))/'Pioneer'/'rekordbox6'/'rekordbox3.settings'
    return Path.home()/'Library'/'Application Support'/'Pioneer'/'rekordbox6'/'rekordbox3.settings'


def played_time(path=None):
    """rekordbox's Playback time preference (seconds before a track enters history), if readable."""
    try:match=re.search(r'name="playedTime" val="(\d+)"',Path(path or settings_file()).read_text(errors='ignore'))
    except OSError:return None
    return int(match.group(1)) if match else None


def database_key():
    # pyrekordbox ships the (local, machine-independent) SQLCipher key for rekordbox 6/7.
    from pyrekordbox.db6.database import BLOB
    from pyrekordbox.utils import deobfuscate
    return deobfuscate(BLOB)


def rekordbox_instance():
    """Identifies the running rekordbox process (pid + start time), or None."""
    try:
        pid=subprocess.run(['pgrep','-x','rekordbox'],capture_output=True,text=True,timeout=3).stdout.split()
        if not pid:return None
        started=subprocess.run(['ps','-o','lstart=','-p',pid[0]],capture_output=True,text=True,timeout=3).stdout.strip()
    except (OSError,subprocess.SubprocessError):return None
    return f'{pid[0]}:{started}'


def parse_time(value):
    try:return datetime.fromisoformat(str(value).replace(' +','+'))
    except ValueError:return None


class Rekordbox:
    def __init__(self,database=None,*,max_age=1800,connect=None,mixer=None,controller=None,
                 state_path=None,instance=rekordbox_instance,clock=time.monotonic,wall=time.time):
        self.database=Path(database) if database else default_database()
        self.share=self.database.parent/'share'
        self.max_age=max_age;self.connect=connect or self._connect
        self.connection=None;self.lock=threading.Lock()
        self.mixer=mixer;self.controller=controller
        self.last_seen=None;self.latest=None;self.reported=None;self.reason=None
        self.clock=clock;self.wall=wall;self.covers={}
        # state_path keeps deck assignments across HueCanvas restarts (same rekordbox run only).
        self.state_path=Path(state_path) if state_path else None;self.instance=instance
        self.restored=False;self.saved_tracks=None

    def _connect(self):
        from sqlcipher3 import dbapi2 as sqlite3
        connection=sqlite3.connect(f'file:{self.database}?mode=ro',uri=True,check_same_thread=False)
        try:
            connection.execute(f"PRAGMA key='{database_key()}'")
            connection.execute('PRAGMA query_only=1')
            connection.execute('select count(*) from sqlite_master').fetchall()
        except Exception:
            connection.close();raise
        return connection

    def query(self,sql,*params):
        with self.lock:
            try:
                if self.connection is None:self.connection=self.connect()
                # fetchall ends the statement, so each poll sees rekordbox's latest WAL commit.
                return self.connection.execute(sql,params).fetchall()
            except Exception:
                self.close_connection();raise

    def close_connection(self):
        if self.connection is not None:
            try:self.connection.close()
            finally:self.connection=None

    async def current_track(self):
        track=await self._current_track()
        self.reason=None if track.get('available') else track.get('reason')
        return track

    async def _current_track(self):
        if self.controller:await asyncio.to_thread(self.controller.poll)
        if self.mixer and not self.restored:
            self.restored=True;await asyncio.to_thread(self.restore_decks)
        if not self.database.exists():return {'available':False,'reason':f'rekordbox library not found at {self.database}'}
        try:rows=await asyncio.to_thread(self.new_rows)
        except ImportError:return {'available':False,'reason':"Install rekordbox support: .venv/bin/python -m pip install -e '.[rekordbox]'"}
        except Exception as exc:return {'available':False,'reason':f'rekordbox library unreadable: {exc}'}
        delay=played_time() if rows and self.mixer else None
        for row in rows:
            content_id,title,artist,album,created=row
            self.latest={'available':True,'id':f'rekordbox:{content_id}','track':title or '','artist':artist,'album':album,'playing':True}
            if self.mixer:self.mixer.assign(self.latest,delay or 0,at=self.row_clock(created))
        if self.mixer:await asyncio.to_thread(self.save_decks)
        if self.mixer and self.mixer.active:
            deck=self.mixer.live();track=self.mixer.track(deck)
            # A live deck rekordbox has not logged yet keeps the previous track's lights.
            if track:self.reported={**track,'deck':deck+1}
            if self.reported:return self.reported
        if self.latest:return self.latest
        seconds=played_time()
        wait=f'rekordbox logs a track after {seconds} s of play' if seconds else 'rekordbox logs a track after its Playback time'
        return {'available':False,'reason':f'Waiting for rekordbox history: {wait} (Preferences > Advanced > Browse > Playback time)'}

    def row_clock(self,created):
        """The mixer clock time at which rekordbox wrote a history row."""
        written=parse_time(created)
        if written is None:return None
        return self.clock()-max(0.0,self.wall()-written.timestamp())

    def restore_decks(self):
        if not self.state_path or not self.state_path.exists():return
        try:state=json.loads(self.state_path.read_text())
        except (OSError,ValueError):return
        current=self.instance()
        # Decks only survive while the same rekordbox process keeps running.
        if not current or state.get('instance')!=current:return
        self.mixer.restore(state.get('decks') or [],max(0.0,self.wall()-float(state.get('saved') or 0)))
        self.saved_tracks=[d.get('track') for d in state.get('decks') or []]

    def save_decks(self):
        if not self.state_path:return
        decks=self.mixer.export();tracks=[d['track'] for d in decks]
        if tracks==self.saved_tracks:return
        instance=self.instance()
        if not instance:return
        self.state_path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        temporary=self.state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'instance':instance,'saved':self.wall(),'decks':decks}))
        temporary.chmod(0o600);temporary.replace(self.state_path)
        self.saved_tracks=tracks

    def new_rows(self):
        if self.last_seen is not None:
            rows=self.query(SINCE,self.last_seen)
            if rows:self.last_seen=rows[-1][4]
            return rows
        rows=self.query(LATEST)
        if not rows:return []
        self.last_seen=rows[0][4]
        # At startup, only a recent row describes this session.
        played=parse_time(rows[0][4])
        if played and (datetime.now(timezone.utc)-played).total_seconds()>self.max_age:return []
        return rows

    def decks(self):
        decks=self.mixer.snapshot() if self.mixer else []
        for deck in decks:
            deck['artwork']=f'/api/decks/{deck["deck"]}/artwork?v={deck["id"].removeprefix("rekordbox:")}' if deck['id'] else None
        return {'history':self.reason,'controller':self.controller.name if self.controller else None,
                'connected':bool(self.controller and self.controller.connected),
                'reason':self.controller.reason if self.controller else None,
                'active':bool(self.mixer and self.mixer.active),'decks':decks,
                'crossfader':self.mixer.crossfader.value if self.mixer else None}

    def image_path(self,content_id):
        rows=self.query('select ImagePath from djmdContent where ID=?',content_id)
        image=rows[0][0] if rows else ''
        if not image:raise ValueError('This track has no artwork in rekordbox')
        source=(self.share/image.lstrip('/')).resolve()
        if not source.is_relative_to(self.share.resolve()):raise ValueError('rekordbox artwork path is outside its share folder')
        return source

    async def deck_artwork(self,number):
        """Cover bytes for the track on deck number (1-based), or None."""
        if not self.mixer or not 1<=number<=len(self.mixer.decks):return None
        track=self.mixer.track(number-1)
        if not track:return None
        content_id=track['id'].removeprefix('rekordbox:')
        if content_id not in self.covers:
            try:path=await asyncio.to_thread(self.image_path,content_id)
            except ValueError:return None
            self.covers[content_id]=await asyncio.to_thread(path.read_bytes)
            while len(self.covers)>8:self.covers.pop(next(iter(self.covers)))
        return self.covers[content_id]

    async def artwork(self,path,track_id=''):
        source=await asyncio.to_thread(self.image_path,track_id.removeprefix('rekordbox:'))
        await asyncio.to_thread(shutil.copyfile,source,path)

    async def close(self):
        if self.controller:self.controller.close()
        await asyncio.to_thread(self._close_locked)

    def _close_locked(self):
        with self.lock:self.close_connection()
