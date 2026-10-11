import ipaddress
from urllib.parse import urlsplit
import httpx

def validate_ip(value):
    value = value.strip()
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        try:
            parsed = urlsplit(value if '://' in value else 'https://' + value)
            if (parsed.scheme != 'https' or parsed.port not in (None, 443)
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
                raise ValueError()
            address = ipaddress.ip_address(parsed.hostname or '')
        except ValueError:
            raise ValueError('Enter a private LAN bridge IP, optionally with HTTPS port :443') from None
    if not address.is_private or address.is_loopback or address.is_multicast or address.is_unspecified:
        raise ValueError('Enter a private LAN bridge IP address')
    return str(address)
async def discover():
    async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
        response = await client.get('https://discovery.meethue.com/')
        response.raise_for_status()
        return response.json()
