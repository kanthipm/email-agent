"""One-time Gmail OAuth: opens a browser, saves the token to credentials/gmail.token.json."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from app import config
from app.mail.gmail import SCOPES


def _flow() -> InstalledAppFlow | None:
    if config.GMAIL_CLIENT_SECRET.exists():
        return InstalledAppFlow.from_client_secrets_file(str(config.GMAIL_CLIENT_SECRET), SCOPES)
    if config.GMAIL_CLIENT_ID and config.GMAIL_CLIENT_SECRET_VALUE:
        return InstalledAppFlow.from_client_config({"installed": {
            "client_id": config.GMAIL_CLIENT_ID,
            "client_secret": config.GMAIL_CLIENT_SECRET_VALUE,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }}, SCOPES)
    return None


def main() -> int:
    flow = _flow()
    if flow is None:
        print(
            "No Gmail OAuth client configured. Either:\n"
            f"  a) download the client JSON from Google Cloud Console to {config.GMAIL_CLIENT_SECRET}, or\n"
            "  b) put GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env (Credentials -> your Desktop client).\n"
            "Then rerun: python scripts/auth_gmail.py",
            file=sys.stderr,
        )
        return 1

    creds = flow.run_local_server(port=0)
    config.CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    config.GMAIL_TOKEN.write_text(creds.to_json())

    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
    address = svc.users().getProfile(userId="me").execute()["emailAddress"]
    print(f"Authenticated as {address}; token saved to {config.GMAIL_TOKEN}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
