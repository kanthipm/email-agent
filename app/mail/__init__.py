"""Registry of configured mail providers."""
from __future__ import annotations

import logging

from app import config
from app.mail.base import MailProvider

log = logging.getLogger(__name__)
_providers: dict[str, MailProvider] | None = None


def providers() -> dict[str, MailProvider]:
    global _providers
    if _providers is None:
        _providers = {}
        if config.GMAIL_ENABLED and config.GMAIL_TOKEN.exists():
            from app.mail.gmail import GmailProvider
            _providers["gmail"] = GmailProvider()
        if config.OUTLOOK_ENABLED and config.OUTLOOK_TOKEN.exists():
            from app.mail.outlook import OutlookProvider
            _providers["outlook"] = OutlookProvider()
        if not _providers:
            log.warning("No mail providers configured. Run scripts/auth_gmail.py and/or scripts/auth_outlook.py")
    return _providers


def get(account: str) -> MailProvider:
    p = providers().get(account)
    if p is None:
        raise KeyError(f"Unknown or unconfigured account: {account}. Available: {list(providers())}")
    return p
