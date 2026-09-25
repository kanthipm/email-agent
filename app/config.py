import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

SENDBLUE_API_KEY = os.getenv("SENDBLUE_API_KEY", "")
SENDBLUE_API_SECRET = os.getenv("SENDBLUE_API_SECRET", "")
SENDBLUE_FROM_NUMBER = os.getenv("SENDBLUE_FROM_NUMBER", "")
SENDBLUE_WEBHOOK_SECRET = os.getenv("SENDBLUE_WEBHOOK_SECRET", "")

MY_PHONE = os.getenv("MY_PHONE", "")
MY_NAME = os.getenv("MY_NAME", "")

GMAIL_ENABLED = _bool("GMAIL_ENABLED", True)
GMAIL_CLIENT_ID = os.getenv("GMAIL_CLIENT_ID", "")
GMAIL_CLIENT_SECRET_VALUE = os.getenv("GMAIL_CLIENT_SECRET", "")
OUTLOOK_ENABLED = _bool("OUTLOOK_ENABLED", True)
OUTLOOK_CLIENT_ID = os.getenv("OUTLOOK_CLIENT_ID", "")
OUTLOOK_TENANT = os.getenv("OUTLOOK_TENANT", "common")

CREDENTIALS_DIR = ROOT / "credentials"
GMAIL_CLIENT_SECRET = CREDENTIALS_DIR / "gmail_client_secret.json"
GMAIL_TOKEN = CREDENTIALS_DIR / "gmail.token.json"
OUTLOOK_TOKEN = CREDENTIALS_DIR / "outlook.token.json"

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "1800"))
QUIET_START_HOUR = int(os.getenv("QUIET_START_HOUR", "22"))
QUIET_END_HOUR = int(os.getenv("QUIET_END_HOUR", "7"))
DB_PATH = ROOT / os.getenv("DB_PATH", "agent.db")
PORT = int(os.getenv("PORT", "8000"))
