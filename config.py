import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
DB_PATH: str = os.getenv("DB_PATH", "watches.db")
AUTH_CODE: str = os.environ["AUTH_CODE"]
ATLAS_PROXY: str = os.getenv("ATLAS_PROXY", "")

OPENROUTER_API_KEY: str = os.environ["OPENROUTER_API_KEY"]
OPENROUTER_MODEL: str = os.environ["OPENROUTER_MODEL"]
OPENROUTER_BASE_URL: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MAX_TURNS: int = int(os.getenv("OPENROUTER_MAX_TURNS", "5"))
LLM_HISTORY_SIZE: int = int(os.getenv("LLM_HISTORY_SIZE", "50"))
LLM_VISION: bool = os.getenv("LLM_VISION", "false").lower() == "true"
LLM_AUDIO: bool = os.getenv("LLM_AUDIO", "false").lower() == "true"
LLM_STT_MODEL: str = os.getenv("LLM_STT_MODEL", "")
