import asyncio
from io import BytesIO
import pytest
from PIL import Image
from fastapi.testclient import TestClient
from app.music.sync import ArtworkSync
from tests.test_music_sync import Provider


def stripes(colors):
    image = Image.new('RGB', (60, 10 * len(colors)))
    for i, color in enumerate(colors): image.paste(color, (0, 10 * i, 60, 10 * i + 10))
    out = BytesIO(); image.save(out, format='PNG'); return out.getvalue()


SIX = ['#d02020', '#20a020', '#2040d0', '#e0c020', '#a020c0', '#20c0c0']


@pytest.fixture
def client(monkeypatch, tmp_path):
    from app import main, config
    monkeypatch.setattr(config, 'MOCK_MODE', True); monkeypatch.setattr(config, 'MUSIC_ENABLED', False)
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path); monkeypatch.setattr(main, 'artwork', None)
    with TestClient(main.app) as client: yield client


def test_changing_palette_size_reclusters_current_artwork(client):
    assert client.post('/api/artwork', files={'file': ('art.png', stripes(SIX), 'image/png')}).status_code == 200
    assert len(client.get('/api/state').json()['palette']) == 4
    revision = client.get('/api/state').json()['music_revision']
    assert client.put('/api/settings', json={'palette_size': 6}).status_code == 200
    state = client.get('/api/state').json()
    assert state['settings']['palette_size'] == 6
    assert sorted(state['palette']) == sorted(SIX)
    assert client.get('/api/state').json()['music_revision'] > revision
    assert client.put('/api/settings', json={'palette_size': 3}).status_code == 200
    assert len(set(client.get('/api/state').json()['palette'])) == 3
    for size in (1, 7):
        assert client.put('/api/settings', json={'palette_size': size}).status_code == 422


def test_upload_count_becomes_the_saved_palette_size(client):
    client.post('/api/artwork?count=6', files={'file': ('art.png', stripes(SIX), 'image/png')})
    state = client.get('/api/state').json()
    assert state['settings']['palette_size'] == 6 and len(state['palette']) == 6


def test_without_artwork_growing_blends_and_shrinking_keeps_edited_swatches(client):
    edited = ['#ff0000', '#0000ff', '#00ff00', '#ffffff']
    client.put('/api/palette', json={'colors': edited})
    client.put('/api/settings', json={'palette_size': 6})
    grown = client.get('/api/state').json()['palette']
    assert len(grown) == 6 and grown[0] == edited[0] and set(edited) - set(grown) <= {'#0000ff', '#00ff00', '#ffffff'}
    client.put('/api/palette', json={'colors': edited})
    client.put('/api/settings', json={'palette_size': 2})
    assert client.get('/api/state').json()['palette'] == edited[:2]


def test_music_sync_extracts_the_selected_palette_size(tmp_path):
    async def scenario():
        size = [6]; events = []
        class Six(Provider):
            async def artwork(self, path, track_id): path.write_bytes(stripes(SIX))
        async def update(kind, value): events.append((kind, value))
        sync = ArtworkSync(Six(), tmp_path, update, palette_size=lambda: size[0])
        try:
            await sync.poll_once(); await sync.worker
            assert len(next(v for k, v in events if k == 'palette')) == 6
        finally: await sync.close()
    asyncio.run(scenario())
