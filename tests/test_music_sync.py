import asyncio
from io import BytesIO
import threading
import pytest
from PIL import Image
from app.music.sync import ArtworkSync
from app.music import apple_music


def image(color):
    out=BytesIO();Image.new('RGB',(20,20),color).save(out,format='PNG');return out.getvalue()


class Provider:
    track={'available':True,'id':'one','track':'First','artist':'Artist','album':'Same album'}
    color='red';calls=0
    async def current_track(self):return dict(self.track)
    async def artwork(self,path,track_id):
        self.calls+=1
        assert track_id==self.track['id']
        path.write_bytes(image(self.color))


def test_track_identity_updates_same_album_artwork_and_uses_cached_artwork(tmp_path):
    async def scenario():
        events=[];provider=Provider()
        async def update(kind,value):events.append((kind,value))
        sync=ArtworkSync(provider,tmp_path,update)
        try:
            await sync.poll_once();await sync.worker
            first=next(v for k,v in events if k=='artwork')
            assert [k for k,v in events]==['track','artwork','palette']
            await sync.poll_once();assert provider.calls==1
            provider.track={**provider.track,'id':'two','track':'Second'};provider.color='blue'
            events.clear();await sync.poll_once();await sync.worker
            assert events[0][1]['track']=='Second'
            assert next(v for k,v in events if k=='artwork')!=first
            provider.track={**provider.track,'id':'one','track':'First'}
            await sync.poll_once();await sync.worker
            assert provider.calls==2  # Cached raw cover avoids another AppleScript request.
        finally:await sync.close()
    asyncio.run(scenario())


def test_cover_is_delivered_before_slow_palette_extraction(tmp_path,monkeypatch):
    import app.music.sync as module
    released=threading.Event();started=threading.Event()
    def extract(data,count):
        started.set();released.wait(2);return ['#ff0000']*4
    monkeypatch.setattr(module,'extract_palette',extract)
    async def scenario():
        events=[];delivered=asyncio.Event()
        async def update(kind,value):
            events.append(kind)
            if kind=='artwork':delivered.set()
        sync=ArtworkSync(Provider(),tmp_path,update)
        try:
            await sync.poll_once();await asyncio.wait_for(delivered.wait(),1)
            assert events==['track','artwork']
            released.set();await sync.worker
            assert events[-1]=='palette'
        finally:released.set();await sync.close()
    asyncio.run(scenario())


def test_rapid_skip_cancels_old_artwork_and_only_publishes_current_cover(tmp_path):
    async def scenario():
        entered=asyncio.Event();cancelled=asyncio.Event();events=[]
        class Slow(Provider):
            async def artwork(self,path,track_id):
                if track_id=='one':
                    entered.set()
                    try:await asyncio.Future()
                    finally:cancelled.set()
                else:await super().artwork(path,track_id)
        provider=Slow()
        async def update(kind,value):events.append((kind,value))
        sync=ArtworkSync(provider,tmp_path,update)
        try:
            await sync.poll_once();await entered.wait()
            provider.track={**provider.track,'id':'two','track':'Second'};provider.color='blue'
            await sync.poll_once();await sync.worker
            assert cancelled.is_set()
            assert [v['track'] for k,v in events if k=='track']==['First','Second']
            assert [v for k,v in events if k=='artwork']==[image('blue')]
            assert not list(tmp_path.glob('.loading-*'))
        finally:await sync.close()
    asyncio.run(scenario())


def test_missing_artwork_is_retried_without_spamming_scripting(tmp_path):
    async def scenario():
        class Missing(Provider):
            async def artwork(self,path,track_id):
                self.calls+=1
                if self.calls==1:raise ValueError('Artwork is still downloading')
                path.write_bytes(image('red'))
        events=[];provider=Missing()
        async def update(kind,value):events.append(kind)
        sync=ArtworkSync(provider,tmp_path,update)
        try:
            await sync.poll_once();await sync.worker
            assert 'error' in events and provider.calls==1
            await sync.poll_once();assert provider.calls==1
            sync.retry_at=0;await sync.poll_once();await sync.worker
            assert sync.loaded and events[-1]=='palette' and provider.calls==2
        finally:await sync.close()
    asyncio.run(scenario())


def test_apple_music_metadata_preserves_empty_album_and_reads_track_identity(monkeypatch):
    async def script(*args,**kwargs):return 'track-id\x1fName\x1fArtist\x1f\x1fpaused'
    monkeypatch.setattr(apple_music,'run_script',script);monkeypatch.setattr(apple_music.sys,'platform','darwin')
    track=asyncio.run(apple_music.AppleMusic().current_track())
    assert track['available'] and track['id']=='track-id' and track['album']=='' and not track['playing']


def test_cancelled_applescript_kills_and_reaps_subprocess(monkeypatch):
    async def scenario():
        started=asyncio.Event();killed=asyncio.Event()
        class Process:
            returncode=None;reaped=False
            async def communicate(self):
                if killed.is_set():self.returncode=-9;self.reaped=True;return b'',b''
                started.set();await asyncio.Future()
            def kill(self):killed.set()
        process=Process()
        async def create(*args,**kwargs):return process
        monkeypatch.setattr(apple_music.asyncio,'create_subprocess_exec',create)
        task=asyncio.create_task(apple_music.run_script('script'))
        await started.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert killed.is_set() and process.reaped
    asyncio.run(scenario())


def test_music_events_publish_latest_snapshot_without_blocking(monkeypatch):
    from app import main
    from app.animation.engine import Engine
    from app.hue.mock import MockClient
    async def scenario():
        queue=asyncio.Queue(maxsize=1)
        monkeypatch.setattr(main,'engine',Engine(MockClient()))
        monkeypatch.setattr(main,'music_subscribers',{queue})
        monkeypatch.setattr(main,'music_revision',0)
        monkeypatch.setattr(main,'music_track',None)
        monkeypatch.setattr(main,'music_error',None)
        monkeypatch.setattr(main,'artwork',None)
        await main.music_update('track',Provider.track)
        await main.music_update('artwork',image('red'))
        payload=queue.get_nowait()
        assert payload['revision']==2 and payload['track']['id']=='one'
        assert payload['artwork'].startswith('/api/artwork?v=')
        assert queue.empty()
    asyncio.run(scenario())


def test_track_change_discards_previous_song_tempo_and_drop_history():
    import numpy as np
    from app.audio.live import LiveBPM
    audio=LiveBPM();audio.running=True;audio.bpm=120;audio.phase_at=100;audio.confidence=.9
    audio.buffer.append(np.ones(120));audio.frames=120;audio.hit_counter=7;audio.drop_counter=2
    audio.hits.append({'id':7,'kind':'percussion','at':100})
    audio.drops.append({'id':2,'kind':'drop','at':100})
    previous=audio.generation;audio.track_changed()
    assert audio.current_bpm() is None and audio.phase_at is None and audio.confidence==0
    assert not audio.buffer and audio.frames==0 and not audio.hits and not audio.drops
    assert audio.hit_counter==7 and audio.drop_counter==2 and audio.generation==previous+1
