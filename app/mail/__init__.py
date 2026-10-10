"""Registry of configured mail providers. A provider whose first connection fails (no network
yet after a wake, token refresh timing out) is retried on the next call instead of being
dropped for the life of the process."""
from __future__ import annotations

import logging

from app import config
from app.mail.base import MailProvider

log = logging.getLogger(__name__)
_providers: dict[str, MailProvider] = {}
_wanted: dict[str, bool] | None = None


def _construct(name: str) -> MailProvider:
    if name == "gmail":
        from app.mail.gmail import GmailProvider
        return GmailProvider()
    from app.mail.outlook import OutlookProvider
    return OutlookProvider()


def providers() -> dict[str, MailProvider]:
    global _wanted
    if _wanted is None:
        _wanted = {"gmail": config.GMAIL_ENABLED and config.GMAIL_TOKEN.exists(),
                   "outlook": config.OUTLOOK_ENABLED and config.OUTLOOK_TOKEN.exists()}
        if not any(_wanted.values()):
            log.warning("No mail providers configured. Run scripts/auth_gmail.py and/or scripts/auth_outlook.py")
    for name, wanted in _wanted.items():
        if wanted and name not in _providers:
            try:
                _providers[name] = _construct(name)
            except Exception as e:
                log.warning("%s provider not ready (%s); will retry", name, e)
    return dict(_providers)


def get(account: str) -> MailProvider:
    p = providers().get(account)
    if p is None:
        raise KeyError(f"Unknown or unconfigured account: {account}. Available: {list(providers())}")
    return p
