import os
from pathlib import Path
from dotenv import load_dotenv
ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / '.env')
MOCK_MODE = os.getenv('MOCK_MODE', 'true').lower() == 'true'
DATA_DIR = ROOT_DIR / '.huecanvas'
MUSIC_ENABLED = os.getenv('APPLE_MUSIC_ENABLED', 'false').lower() == 'true'
