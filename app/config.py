import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# Any OpenAI-compatible chat API. Presets: (base_url, default model, default fallback model, key env var)
LLM_PRESETS = {
    "groq": ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b", "openai/gpt-oss-20b", "GROQ_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.8-flash",
               "gemini-3.5-flash-lite", "GEMINI_API_KEY"),
    "cerebras": ("https://api.cerebras.ai/v1", "gpt-oss-120b", "llama-3.3-70b", "CEREBRAS_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "openai/gpt-oss-120b:free",
                   "meta-llama/llama-3.3-70b-instruct:free", "OPENROUTER_API_KEY"),
}
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq").strip().lower()
_preset = LLM_PRESETS.get(LLM_PROVIDER, LLM_PRESETS["groq"])
LLM_BASE_URL = os.getenv("LLM_BASE_URL", _preset[0]).rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL") or os.getenv("GROQ_MODEL", _preset[1])
LLM_FALLBACK_MODEL = os.getenv("LLM_FALLBACK_MODEL") or os.getenv("GROQ_FALLBACK_MODEL", _preset[2])
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv(_preset[3], "")


def _chain() -> list[tuple[str, str, str]]:
    """(base_url, api_key, model) entries tried in order when a model's daily quota is exhausted:
    the chosen provider's main and fallback models, then every other provider that has a key."""
    out = [(LLM_BASE_URL, LLM_API_KEY, LLM_MODEL), (LLM_BASE_URL, LLM_API_KEY, LLM_FALLBACK_MODEL)]
    for name, (url, main, fallback, key_env) in LLM_PRESETS.items():
        key = os.getenv(key_env, "")
        if name != LLM_PROVIDER and key:
            out += [(url, key, main), (url, key, fallback)]
    return [e for i, e in enumerate(out) if e[1] and e[2] and e not in out[:i]]


LLM_CHAIN = _chain()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")   # still used for the job digest's web search (Groq-only tool)

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
JOBS_DIGEST_ENABLED = _bool("JOBS_DIGEST_ENABLED", True)
JOBS_DIGEST_HOUR = int(os.getenv("JOBS_DIGEST_HOUR", "8"))
JOBS_DIGEST_TO = os.getenv("JOBS_DIGEST_TO", "")
JOBS_PROFILE = os.getenv("JOBS_PROFILE", "")
JOBS_WEB_SEARCH = _bool("JOBS_WEB_SEARCH", True)
JOBS_STRICT_NEW_GRAD = _bool("JOBS_STRICT_NEW_GRAD", True)
JOBS_SEARCH_MODEL = os.getenv("JOBS_SEARCH_MODEL", "openai/gpt-oss-20b")

DB_PATH = ROOT / os.getenv("DB_PATH", "agent.db")
PORT = int(os.getenv("PORT", "8000"))
