import os
from pathlib import Path
from dotenv import load_dotenv
ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / '.env')
MOCK_MODE = os.getenv('MOCK_MODE', 'true').lower() == 'true'
DATA_DIR = ROOT_DIR / '.huecanvas'
REKORDBOX_ENABLED = os.getenv('REKORDBOX_ENABLED', 'false').lower() == 'true'
APPLE_MUSIC_ENABLED = os.getenv('APPLE_MUSIC_ENABLED', 'false').lower() == 'true'
# One track source drives artwork; rekordbox wins when both are enabled.
MUSIC_SOURCE = 'rekordbox' if REKORDBOX_ENABLED else 'apple_music' if APPLE_MUSIC_ENABLED else None
MUSIC_ENABLED = MUSIC_SOURCE is not None
# MIDI port name (substring) of the DJ controller used to tell which deck is live; empty disables.
REKORDBOX_CONTROLLER = os.getenv('REKORDBOX_CONTROLLER', 'DDJ-FLX4').strip()
