"""One-time Outlook login via MSAL device code; saves the token cache to credentials/outlook.token.json."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import msal

from app import config
from app.mail.outlook import GRAPH, SCOPES


def main() -> int:
    if not config.OUTLOOK_CLIENT_ID:
        print(
            "OUTLOOK_CLIENT_ID is not set.\n\n"
            "Register an app in Microsoft Entra:\n"
            "  1. https://entra.microsoft.com -> App registrations -> New registration.\n"
            "  2. Supported account types: \"Personal Microsoft accounts and any org\" (or as needed).\n"
            "     No redirect URI is required for device code flow.\n"
            "  3. Authentication -> Advanced settings -> \"Allow public client flows\" = Yes -> Save.\n"
            "  4. API permissions -> Add -> Microsoft Graph -> Delegated: Mail.ReadWrite, Mail.Send, User.Read.\n"
            "  5. Copy the Application (client) ID into .env as OUTLOOK_CLIENT_ID.\n"
            "Then rerun: python scripts/auth_outlook.py",
            file=sys.stderr,
        )
        return 1

    cache = msal.SerializableTokenCache()
    app = msal.PublicClientApplication(
        config.OUTLOOK_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{config.OUTLOOK_TENANT}",
        token_cache=cache,
    )
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        print(f"Could not start device flow: {flow.get('error_description') or flow}", file=sys.stderr)
        return 1
    print(flow["message"], flush=True)

    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        print(f"Login failed: {result.get('error_description') or result}", file=sys.stderr)
        return 1

    config.CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    config.OUTLOOK_TOKEN.write_text(cache.serialize())

    r = httpx.get(f"{GRAPH}/me", headers={"Authorization": f"Bearer {result['access_token']}"}, timeout=30)
    r.raise_for_status()
    me = r.json()
    address = me.get("mail") or me.get("userPrincipalName")
    print(f"Authenticated as {address}; token saved to {config.OUTLOOK_TOKEN}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
