import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from io import BytesIO
import pytest
from PIL import Image
from app.music.rekordbox import Rekordbox, played_time
from app.music.sync import ArtworkSync


def image(color):
    out=BytesIO();Image.new('RGB',(20,20),color).save(out,format='JPEG');return out.getvalue()


def stamp(ago=0):
    # rekordbox's own created_at format.
    moment=datetime.now(timezone.utc)-timedelta(seconds=ago)
    return moment.strftime('%Y-%m-%d %H:%M:%S.')+f'{moment.microsecond//1000:03d} +00:00'


@pytest.fixture
def library(tmp_path):
    db=tmp_path/'master.db'
    c=sqlite3.connect(db)
    c.executescript('''create table djmdContent(ID text,Title text,ArtistID text,AlbumID text,ImagePath text,rb_local_deleted int default 0);
create table djmdArtist(ID text,Name text);create table djmdAlbum(ID text,Name text);
create table djmdSongHistory(ID text,HistoryID text,ContentID text,TrackNo int,rb_local_deleted int,created_at text);
insert into djmdArtist values('a1','Artist One');insert into djmdAlbum values('b1','Album One');''')
    c.commit();c.close()
    (tmp_path/'share'/'PIONEER'/'Artwork').mkdir(parents=True)
    return tmp_path


def add(root,content,title,art=None,color='red',ago=0,deleted=0,artist='a1',album='b1'):
    c=sqlite3.connect(root/'master.db')
    path=''
    if art:
        path=f'/PIONEER/Artwork/{art}.jpg';(root/'share'/path.lstrip('/')).write_bytes(image(color))
    if not c.execute('select 1 from djmdContent where ID=?',(content,)).fetchone():
        c.execute('insert into djmdContent(ID,Title,ArtistID,AlbumID,ImagePath) values(?,?,?,?,?)',(content,title,artist,album,path))
    c.execute('insert into djmdSongHistory values(?,?,?,?,?,?)',(f'row-{content}-{ago}','h1',content,1,deleted,stamp(ago)))
    c.commit();c.close()


def provider(root,**kwargs):
    return Rekordbox(root/'master.db',connect=lambda:sqlite3.connect(root/'master.db',check_same_thread=False),**kwargs)


def test_latest_history_row_becomes_current_track(library):
    add(library,'1','Older',ago=300);add(library,'2','Newest',ago=5)
    add(library,'3','Removed from history',deleted=1)
    track=asyncio.run(provider(library).current_track())
    assert track=={'available':True,'id':'rekordbox:2','track':'Newest','artist':'Artist One','album':'Album One','playing':True}


def test_missing_artist_and_album_are_empty(library):
    add(library,'1','Untagged',artist=None,album=None)
    track=asyncio.run(provider(library).current_track())
    assert track['artist']=='' and track['album']==''


def test_stale_or_empty_history_is_unavailable(library):
    assert not asyncio.run(provider(library).current_track())['available']
    add(library,'1','Last set',ago=3*3600)
    track=asyncio.run(provider(library).current_track())
    assert not track['available'] and 'Waiting for rekordbox history' in track['reason']


def test_missing_library_reports_reason(tmp_path):
    track=asyncio.run(Rekordbox(tmp_path/'nope.db').current_track())
    assert not track['available'] and 'not found' in track['reason']


def test_artwork_is_copied_from_share_folder(library,tmp_path):
    add(library,'1','Song',art='one',color='blue')
    out=tmp_path/'cover.art'
    asyncio.run(provider(library).artwork(out,'rekordbox:1'))
    assert out.read_bytes()==image('blue')


def test_track_without_artwork_or_outside_share_is_rejected(library,tmp_path):
    add(library,'1','No cover')
    with pytest.raises(ValueError,match='no artwork'):asyncio.run(provider(library).artwork(tmp_path/'x','rekordbox:1'))
    c=sqlite3.connect(library/'master.db')
    c.execute("insert into djmdContent(ID,Title,ArtistID,AlbumID,ImagePath) values('2','Escape','a1','b1','/../../outside.jpg')");c.commit();c.close()
    with pytest.raises(ValueError,match='outside'):asyncio.run(provider(library).artwork(tmp_path/'x','rekordbox:2'))


def test_new_history_row_switches_artwork_through_sync(library,tmp_path):
    async def scenario():
        events=[];rb=provider(library)
        async def update(kind,value):events.append((kind,value))
        sync=ArtworkSync(rb,tmp_path/'cache',update)
        try:
            add(library,'1','First',art='one',color='red',ago=60)
            await sync.poll_once();await sync.worker
            add(library,'2','Second',art='two',color='blue')
            await sync.poll_once();await sync.worker
            assert [v['track'] for k,v in events if k=='track']==['First','Second']
            covers=[v for k,v in events if k=='artwork']
            assert covers==[image('red'),image('blue')]
            assert [k for k,v in events].count('palette')==2
        finally:await sync.close();await rb.close()
    asyncio.run(scenario())


def test_connection_is_reopened_after_read_error(library):
    add(library,'1','Song')
    opened=[]
    def connect():
        opened.append(1)
        if len(opened)==1:raise sqlite3.OperationalError('database is locked')
        return sqlite3.connect(library/'master.db',check_same_thread=False)
    rb=Rekordbox(library/'master.db',connect=connect)
    assert not asyncio.run(rb.current_track())['available']
    assert asyncio.run(rb.current_track())['available'] and len(opened)==2


def test_playback_time_is_read_from_rekordbox_settings(tmp_path):
    settings=tmp_path/'rekordbox3.settings'
    settings.write_text('<PROPERTIES>\r\n  <VALUE name="JogStartTime" val="0"/>\r\n  <VALUE name="playedTime" val="15"/>\r\n</PROPERTIES>')
    assert played_time(settings)==15
    assert played_time(tmp_path/'missing')is None


def test_waiting_reason_is_exposed_for_the_dashboard(library):
    rb=provider(library)
    asyncio.run(rb.current_track())
    assert 'Waiting for rekordbox history' in rb.decks()['history'] and rb.decks()['decks']==[]
    add(library,'1','Song');asyncio.run(rb.current_track())
    assert rb.decks()['history'] is None
