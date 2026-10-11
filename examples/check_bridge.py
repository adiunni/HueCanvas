"""Read-only bridge check: run with .venv/bin/python examples/check_bridge.py."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import config
from app.main import real_client

async def main():
    if config.MOCK_MODE:
        print('MOCK_MODE is true. Set MOCK_MODE=false in .env and restart the server.')
        return
    client = None
    try:
        client = real_client()
        if not client.http.headers.get('hue-application-key'):
            # Probe HTTPS even before pairing, using the selected TLS policy.
            await client.request('GET', '/api/config')
            print('HTTPS connection to bridge succeeded.')
            print('No application key configured. Press the bridge button and click Pair in HueCanvas.')
            return 0
        devices = await client.request('GET', '/clip/v2/resource/device/')
        print(f'Authenticated device request succeeded; discovered {len(devices.get("data", []))} devices.')
        print('Certificate verification:', 'enabled' if client.verify_tls else 'disabled for local bridge')
        lights = await client.discover_lights()
        print(f'Authenticated successfully; discovered {len(lights)} lights.')
        print('Enable your four bulbs with Enable bulb in the dashboard before Start.')
    except Exception as exc:
        print(f'Bridge check failed: {exc}')
        return 1
    finally:
        if client is not None: await client.close()

if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()) or 0)
