import asyncio
import ssl
import httpx
import pytest
from app.hue.client import HueClient
from app.hue.discovery import validate_ip

@pytest.mark.parametrize('ip',['127.0.0.1','8.8.8.8','0.0.0.0','224.0.0.1','not-an-ip'])
def test_reject_non_lan_bridges(ip):
    with pytest.raises(ValueError): validate_ip(ip)

def test_v2_resource_capabilities_and_verified_identity(monkeypatch):
    context = ssl.create_default_context()
    monkeypatch.setattr('app.hue.client.ssl.create_default_context',lambda **kwargs:context)
    seen=[]
    def respond(request):
        seen.append(request)
        assert request.extensions['sni_hostname']=='001788fffe123456'
        assert request.headers['hue-application-key']=='test-key'
        return httpx.Response(200,json={'errors':[],'data':[
            {'id':'color','metadata':{'name':'Color'},'on':{'on':True},'dimming':{'brightness':42},'color':{'xy':{'x':.3,'y':.4},'gamut':{'red':{'x':.7,'y':.3},'green':{'x':.2,'y':.7},'blue':{'x':.1,'y':.1}}}},
            {'id':'white','metadata':{'name':'White'},'on':{'on':False},'dimming':{'brightness':30}},
            {'id':'ct','metadata':{'name':'Temperature'},'color_temperature':{'mirek':250}}
        ]})
    async def scenario():
        client=HueClient('192.168.1.2','test-key',verify_tls=True,bridge_id='001788fffe123456',ca_cert='trusted.pem')
        await client.http.aclose()
        client.http=httpx.AsyncClient(base_url='https://192.168.1.2',transport=httpx.MockTransport(respond),headers={'hue-application-key':'test-key'})
        lights=await client.discover_lights()
        assert lights[0].color and lights[0].dimming and lights[0].gamut
        assert not lights[1].color and not lights[1].temperature
        assert lights[2].temperature and not lights[2].dimming
        assert not any(light.enabled for light in lights)
        assert seen[0].url.path=='/clip/v2/resource/light'
        await client.close()
    asyncio.run(scenario())
    assert context.check_hostname and context.verify_mode==ssl.CERT_REQUIRED

@pytest.mark.parametrize('value',['192.168.1.2','192.168.1.2:443','https://192.168.1.2:443/','https://192.168.1.2'])
def test_accept_https_bridge_address(value):
    assert validate_ip(value)=='192.168.1.2'

@pytest.mark.parametrize('value',['http://192.168.1.2','192.168.1.2:80','https://192.168.1.2/path','https://user:password@192.168.1.2','https://127.0.0.1:443'])
def test_reject_unsafe_bridge_urls(value):
    with pytest.raises(ValueError): validate_ip(value)

def test_pairing_and_connection_errors_are_actionable(monkeypatch):
    context=ssl.create_default_context()
    monkeypatch.setattr('app.hue.client.ssl.create_default_context',lambda **kwargs:context)
    async def scenario():
        client=HueClient('192.168.1.2:443',verify_tls=True,bridge_id='001788fffe123456',ca_cert='trusted.pem')
        await client.http.aclose()
        seen=[]
        def unauthenticated(request):
            seen.append(request)
            return httpx.Response(403)
        client.http=httpx.AsyncClient(base_url='https://192.168.1.2',transport=httpx.MockTransport(unauthenticated))
        with pytest.raises(ValueError,match='No Hue application key'):
            await client.discover_lights()
        assert not seen
        client.http.headers['hue-application-key']='invalid-test-key'
        with pytest.raises(ValueError,match='rejected the application key'):
            await client.discover_lights()
        await client.http.aclose()
        def unreachable(request):
            raise httpx.ConnectError('All connection attempts failed',request=request)
        client.http=httpx.AsyncClient(base_url='https://192.168.1.2',transport=httpx.MockTransport(unreachable))
        with pytest.raises(ValueError,match='Cannot reach the Hue Bridge'):
            await client.pair()
        await client.close()
    asyncio.run(scenario())

def test_local_bridge_uses_only_ip_and_key(monkeypatch):
    def unexpected_ca_load(**kwargs):
        raise AssertionError('Local mode must not load a certificate file')
    monkeypatch.setattr('app.hue.client.ssl.create_default_context',unexpected_ca_load)
    async def scenario():
        client=HueClient('192.168.1.2:443','existing-key',bridge_id='unused',ca_cert='missing.pem')
        assert not client.verify_tls and client.bridge_id==''
        context=client.http._transport._pool._ssl_context
        assert context.verify_mode==ssl.CERT_NONE and not context.check_hostname
        await client.http.aclose()
        requests=[]
        def respond(request):
            requests.append(request)
            assert request.headers['hue-application-key']=='existing-key'
            assert 'sni_hostname' not in request.extensions
            return httpx.Response(200,json={'errors':[],'data':[]})
        client.http=httpx.AsyncClient(base_url='https://192.168.1.2',headers={'hue-application-key':'existing-key'},transport=httpx.MockTransport(respond))
        await client.request('GET','/clip/v2/resource/device/')
        assert requests[0].url.host=='192.168.1.2'
        await client.close()
    asyncio.run(scenario())

def test_verified_mode_requires_identity_and_ca():
    with pytest.raises(ValueError,match='HUE_TLS_VERIFY=true requires'):
        HueClient('192.168.1.2','test-key',verify_tls=True)

def test_pairing_label_and_returned_username():
    import json
    async def scenario():
        client=HueClient('192.168.1.2')
        await client.http.aclose()
        def respond(request):
            assert request.method=='POST' and request.url.path=='/api'
            assert json.loads(request.content)=={'devicetype':'hue_canvas'}
            return httpx.Response(200,json=[{'success':{'username':'new-application-key'}}])
        client.http=httpx.AsyncClient(base_url='https://192.168.1.2',transport=httpx.MockTransport(respond))
        assert await client.pair()=='new-application-key'
        assert client.http.headers['hue-application-key']=='new-application-key'
        await client.close()
    asyncio.run(scenario())

def test_env_connection_does_not_require_bridge_id_or_pem(monkeypatch, tmp_path):
    from app import config
    from app.main import real_client, Pair
    monkeypatch.setattr(config,'DATA_DIR',tmp_path)
    monkeypatch.setenv('HUE_BRIDGE_IP','192.168.1.2')
    monkeypatch.setenv('HUE_APPLICATION_KEY','test-key')
    monkeypatch.delenv('HUE_TLS_VERIFY',raising=False)
    monkeypatch.setenv('HUE_BRIDGE_ID','unused')
    monkeypatch.setenv('HUE_CA_CERT','missing.pem')
    async def scenario():
        client=real_client()
        assert not client.verify_tls
        assert client.http.headers['hue-application-key']=='test-key'
        await client.close()
    asyncio.run(scenario())
    assert Pair(ip='192.168.1.2').bridge_id==''

@pytest.mark.parametrize('method,path',[
    ('GET','/clip/v2/resource/device/'),
    ('GET','/clip/v2/resource/light'),
    ('PUT','/clip/v2/resource/light/example-light'),
])
def test_actual_client_attaches_application_key_before_transport(method,path):
    # Keep the real AsyncClient and its default headers; intercept after HTTPX
    # prepares the request, before attempting any hardware/network connection.
    class RequestCaptured(Exception):
        pass
    captured=[]
    async def intercept(request):
        captured.append(request)
        raise RequestCaptured()
    async def scenario():
        client=HueClient('192.168.1.2','test-application-key')
        client.http.event_hooks['request'].append(intercept)
        try:
            with pytest.raises(RequestCaptured):
                await client.request(method,path)
            request=captured[0]
            assert request.method==method
            assert str(request.url)=='https://192.168.1.2'+path
            assert request.headers['hue-application-key']=='test-application-key'
        finally:
            await client.close()
    asyncio.run(scenario())
