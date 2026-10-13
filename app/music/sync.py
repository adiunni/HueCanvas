"""Fast track polling, independent artwork delivery, and cancellation on skips."""
import asyncio
import hashlib
import time
from collections import OrderedDict
from contextlib import suppress
from io import BytesIO
from pathlib import Path
from PIL import Image, ImageOps
from app.palette.extract import extract_palette


def identity(track):
    return tuple(track.get(k,'') for k in ('id','track','artist','album'))


def normalize_artwork(data):
    if len(data)>10*1024*1024:raise ValueError('Artwork too large')
    with Image.open(BytesIO(data)) as image:
        if image.format in ('JPEG','PNG'):
            image.verify();return data
        out=BytesIO();ImageOps.exif_transpose(image).convert('RGB').save(out,format='PNG')
        result=out.getvalue()
        if len(result)>10*1024*1024:raise ValueError('Artwork too large')
        return result


class ArtworkSync:
    def __init__(self,provider,directory,on_update,*,poll_interval=.5,palette_size=lambda:4,on_transport=None):
        self.provider=provider;self.directory=Path(directory);self.on_update=on_update;self.palette_size=palette_size
        self.poll_interval=poll_interval;self.current=None;self.generation=0
        self.worker=None;self.loaded=False;self.retry_at=0;self.palette_cache=OrderedDict()
        self.on_transport=on_transport

    async def poll_once(self):
        track=await self.provider.current_track()
        if not track.get('available'):
            if self.on_transport:await self.on_transport(track)
            return
        key=identity(track)
        if key!=self.current:
            self.current=key;self.generation+=1;self.loaded=False;self.retry_at=0
            if self.worker:
                self.worker.cancel()
                with suppress(asyncio.CancelledError):await self.worker
            await self.on_update('track',track)
        if self.on_transport:await self.on_transport(track)
        if not self.loaded and (not self.worker or self.worker.done()) and time.monotonic()>=self.retry_at:
            self.worker=asyncio.create_task(self.load(track,key,self.generation))

    async def load(self,track,key,generation):
        digest=hashlib.sha256(repr(key).encode()).hexdigest()
        path=self.directory/f'track-{digest}.art';temporary=self.directory/f'.loading-{digest}.art'
        try:
            self.directory.mkdir(mode=0o700,exist_ok=True)
            if path.exists():data=path.read_bytes()
            else:
                await self.provider.artwork(temporary,track.get('id',''))
                data=temporary.read_bytes()
            try:data=await asyncio.to_thread(normalize_artwork,data)
            except Exception:
                path.unlink(missing_ok=True)
                raise
            if generation!=self.generation:return
            path.write_bytes(data)
            # Deliver the image before doing the slower palette clustering.
            await self.on_update('artwork',data)
            size=self.palette_size()
            artwork_key=(hashlib.sha256(data).hexdigest(),size)
            if artwork_key not in self.palette_cache:
                colors=await asyncio.to_thread(extract_palette,data,size)
                self.palette_cache[artwork_key]=colors
                while len(self.palette_cache)>128:self.palette_cache.popitem(last=False)
            if generation!=self.generation:return
            await self.on_update('palette',self.palette_cache[artwork_key])
            self.loaded=True
        except asyncio.CancelledError:raise
        except Exception as exc:
            if generation==self.generation:
                self.retry_at=time.monotonic()+2
                await self.on_update('error',str(exc))
        finally:temporary.unlink(missing_ok=True)

    async def close(self):
        if self.worker:
            self.worker.cancel()
            with suppress(asyncio.CancelledError):await self.worker
            self.worker=None

    async def run(self):
        try:
            while True:
                began=time.monotonic()
                try:await self.poll_once()
                except Exception as exc:await self.on_update('error',str(exc))
                await asyncio.sleep(max(.05,self.poll_interval-(time.monotonic()-began)))
        finally:await self.close()
