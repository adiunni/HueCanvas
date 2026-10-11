import asyncio
import os
import struct
from uuid import UUID
from app.hue.dtls import DTLSTransport
from app.hue.mock import MockClient

STREAM_MODES = ('beat_gentle', 'beat_flash')
AVAILABLE = True


def encode_frame(configuration_id, sequence, channels):
    UUID(configuration_id)
    header = b'HueStream' + bytes([2, 0, sequence % 256, 0, 0, 1, 0])
    body = bytearray(header + configuration_id.encode('ascii'))
    for channel, (x, y, brightness) in sorted(channels.items()):
        if not 0 <= channel <= 255: raise ValueError('Invalid entertainment channel')
        values = [round(max(0, min(1, v))*65535) for v in (x, y, brightness)]
        body.extend(struct.pack('!BHHH',channel,*values))
    return bytes(body)


def select_area(configs, services, lights, requested_id=None):
    selected = {light.id: light for light in lights if light.enabled and light.color}
    owners = {light.owner: light.id for light in selected.values() if light.owner}
    service_owners = {r['id']: r.get('owner',{}).get('rid') for r in services}
    for area in configs:
        if requested_id and area['id'] != requested_id: continue
        mapping = {}; valid = True
        for channel in area.get('channels',[]):
            members = channel.get('members',[])
            if len(members)!=1:
                valid=False; break
            service = members[0].get('service',{})
            light_id = owners.get(service_owners.get(service.get('rid')))
            if light_id is None:
                valid=False; break
            mapping[channel['channel_id']] = light_id
        if valid and mapping and set(mapping.values())==set(selected):
            if area.get('status')=='active': raise ValueError('The Music area is already active in another app. Stop Hue Sync before starting HueCanvas.')
            return area['id'], mapping
    raise ValueError('Choose an Entertainment area containing exactly the enabled color bulbs. Configure it in the Hue app; white bulbs stay on REST.')


class EntertainmentSession:
    def __init__(self, client, transport_factory=DTLSTransport):
        self.client = client; self.transport_factory = transport_factory
        self.transport = None; self.configuration_id = None; self.channels = {}; self.sequence = 0
        self.activated = False; self.mock = isinstance(client,MockClient); self.frames = []

    async def start(self, lights):
        colors = [light for light in lights if light.enabled and light.color]
        if not colors: raise ValueError('Enable at least one color bulb for beat effects')
        if self.mock:
            self.configuration_id = '00000000-0000-0000-0000-000000000001'
            self.channels = {i:l.id for i,l in enumerate(colors)}
            return
        if not self.client.client_key:
            raise ValueError('Beat effects require HUE_CLIENT_KEY from the same pairing response as HUE_APPLICATION_KEY. Add it to .env and restart.')
        configs = await self.client.request('GET','/clip/v2/resource/entertainment_configuration')
        services = await self.client.request('GET','/clip/v2/resource/entertainment')
        self.configuration_id, self.channels = select_area(configs['data'],services['data'],lights,os.getenv('HUE_ENTERTAINMENT_AREA') or None)
        try:
            for light in colors:
                await self.client.update(light.id,{'on':{'on':True},'dynamics':{'duration':1000}})
            await self.client.request('PUT',f'/clip/v2/resource/entertainment_configuration/{self.configuration_id}',json={'action':'start'})
            self.activated = True
            self.transport = self.transport_factory()
            await asyncio.to_thread(self.transport.connect, self.client.ip, self.client.http.headers['hue-application-key'], self.client.client_key)
        except BaseException:
            await self.stop(); raise

    async def send(self, values):
        channels = {channel:values[light_id] for channel,light_id in self.channels.items()}
        packet = encode_frame(self.configuration_id,self.sequence,channels); self.sequence=(self.sequence+1)%256
        if self.mock:
            self.frames.append(packet); self.frames=self.frames[-100:]
            for light in self.client.lights:
                if light.id in values:
                    x,y,b=values[light.id];light.on=True;light.xy=[x,y];light.brightness=b*100
        else:
            await asyncio.to_thread(self.transport.send,packet)

    async def stop(self):
        try:
            if self.transport:
                transport=self.transport;self.transport=None
                await asyncio.to_thread(transport.close)
        finally:
            if self.activated:
                self.activated=False
                await self.client.request('PUT',f'/clip/v2/resource/entertainment_configuration/{self.configuration_id}',json={'action':'stop'})
