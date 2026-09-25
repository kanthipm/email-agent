"""One-time Gmail OAuth: opens a browser, saves the token to credentials/gmail.token.json."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from app import config
from app.mail.gmail import SCOPES


def main() -> int:
    if not config.GMAIL_CLIENT_SECRET.exists():
        print(
            f"Missing {config.GMAIL_CLIENT_SECRET}\n\n"
            "Create an OAuth client in Google Cloud Console:\n"
            "  1. Enable the Gmail API for your project.\n"
            "  2. APIs & Services -> Credentials -> Create credentials -> OAuth client ID -> Desktop app.\n"
            "  3. Download the JSON and save it as credentials/gmail_client_secret.json.\n"
            "Then rerun: python scripts/auth_gmail.py",
            file=sys.stderr,
        )
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(config.GMAIL_CLIENT_SECRET), SCOPES)
    creds = flow.run_local_server(port=0)
    config.CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    config.GMAIL_TOKEN.write_text(creds.to_json())

    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
    address = svc.users().getProfile(userId="me").execute()["emailAddress"]
    print(f"Authenticated as {address}; token saved to {config.GMAIL_TOKEN}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
