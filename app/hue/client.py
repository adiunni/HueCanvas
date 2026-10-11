import asyncio
import ssl
import time
import httpx
from app.hue.models import Light
from app.hue.discovery import validate_ip
class HueClient:
    def __init__(self, ip, key='', *, verify_tls=False, bridge_id='', ca_cert='', client_key=''):
        ip = validate_ip(ip)
        self.ip = ip; self.client_key = client_key.strip()
        self.verify_tls = verify_tls
        self.bridge_id = bridge_id.strip().lower() if verify_tls else ''
        context = False
        if verify_tls:
            if not self.bridge_id or not ca_cert:
                raise ValueError('HUE_TLS_VERIFY=true requires HUE_BRIDGE_ID and HUE_CA_CERT')
            context = ssl.create_default_context(cafile=ca_cert)
        self.http = httpx.AsyncClient(base_url=f'https://{"["+ip+"]" if ":" in ip else ip}', verify=context, timeout=8, trust_env=False, headers={'hue-application-key': key}, transport=httpx.AsyncHTTPTransport(verify=context, retries=1))
        self.lock = asyncio.Lock(); self.last = 0
    async def request(self, method, path, **kwargs):
        async with self.lock:
            await asyncio.sleep(max(0, .25-(time.monotonic()-self.last)))
            self.last = time.monotonic()
            try:
                extensions = {'sni_hostname': self.bridge_id} if self.verify_tls else {}
                response = await self.http.request(method, path, extensions=extensions, **kwargs)
            except httpx.ConnectError as exc:
                if 'CERTIFICATE_VERIFY_FAILED' in str(exc):
                    raise ValueError('Bridge TLS verification failed. Check HUE_CA_CERT, HUE_BRIDGE_ID, and the Mac clock; certificate verification remains enabled.') from exc
                raise ValueError('Cannot reach the Hue Bridge. Check its power, the LAN address, Wi-Fi/VPN routing, and Local Network access for the app launching HueCanvas.') from exc
            except httpx.TimeoutException as exc:
                raise ValueError('The Hue Bridge timed out. Check its network connection, then use Refresh to retry.') from exc
            if response.status_code in (401, 403):
                raise ValueError('The bridge rejected the application key. Press its physical button, then click Pair to generate a new key.')
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and data.get('errors'):
                raise ValueError(str(data['errors']))
            return data
    async def pair(self):
        result = await self.request('POST', '/api', json={'devicetype':'hue_canvas'})
        if not result or 'success' not in result[0]:
            raise ValueError('Press the physical bridge button, then retry pairing')
        key = result[0]['success']['username']
        self.http.headers['hue-application-key'] = key
        self.client_key = ''  # A key from another pairing cannot authenticate this identity.
        return key
    async def discover_lights(self):
        if not self.http.headers.get('hue-application-key'):
            raise ValueError('No Hue application key is configured. Press the physical bridge button, then click Pair; HueCanvas will save the generated key securely.')
        data = await self.request('GET', '/clip/v2/resource/light')
        lights = []
        for r in data['data']:
            gamut = r.get('color', {}).get('gamut')
            lights.append(Light(id=r['id'], name=r.get('metadata', {}).get('name','Hue light'), owner=r.get('owner',{}).get('rid'), color='color' in r, dimming='dimming' in r, temperature='color_temperature' in r, gamut=[[gamut[k]['x'],gamut[k]['y']] for k in ('red','green','blue')] if gamut else None, on=r.get('on',{}).get('on',False), brightness=r.get('dimming',{}).get('brightness',50), xy=list(r['color']['xy'].values()) if 'color' in r else None))
        return lights
    async def update(self, light_id, payload):
        await self.request('PUT', f'/clip/v2/resource/light/{light_id}', json=payload)
    async def close(self): await self.http.aclose()
