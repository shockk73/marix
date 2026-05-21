import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
DB_PATH: str = os.getenv("DB_PATH", "watches.db")
AUTH_CODE: str = os.environ["AUTH_CODE"]
ATLAS_PROXY: str = os.getenv("ATLAS_PROXY", "")
