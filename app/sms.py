"""Sendblue transport: outbound texts and inbound webhook parsing.

Sendblue does not sign webhook bodies. It echoes the account's signing secret in an
sb-signing-secret header, or the secret can be the last path segment of the registered URL.
"""
from __future__ import annotations

import hmac
import logging
from typing import Any

import httpx

from app import config

log = logging.getLogger(__name__)
SEND_URL = "https://api.sendblue.co/api/send-message"
MAX_CHUNK = 1500


def configured() -> bool:
    return bool(config.SENDBLUE_API_KEY and config.SENDBLUE_API_SECRET and config.MY_PHONE)


def _chunks(text: str) -> list[str]:
    text = text.strip()
    if len(text) <= MAX_CHUNK:
        return [text]
    out, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 > MAX_CHUNK and cur:
            out.append(cur)
            cur = ""
        while len(para) > MAX_CHUNK:
            out.append(para[:MAX_CHUNK])
            para = para[MAX_CHUNK:]
        cur = f"{cur}\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out


def send_text(content: str, to: str | None = None) -> None:
    """Text the user. Splits long messages. Raises httpx.HTTPError on failure."""
    to = to or config.MY_PHONE
    if not configured():
        log.info("SMS stub -> %s: %s", to, content)
        return
    headers = {"sb-api-key-id": config.SENDBLUE_API_KEY, "sb-api-secret-key": config.SENDBLUE_API_SECRET}
    for part in _chunks(content):
        payload: dict[str, Any] = {"number": to, "content": part}
        if config.SENDBLUE_FROM_NUMBER:
            payload["from_number"] = config.SENDBLUE_FROM_NUMBER
        r = httpx.post(SEND_URL, headers=headers, json=payload, timeout=10.0)
        r.raise_for_status()


def secret_ok(header_value: str | None, path_value: str | None) -> bool:
    want = config.SENDBLUE_WEBHOOK_SECRET
    if not want:
        return False
    return any(v and hmac.compare_digest(v.encode(), want.encode()) for v in (header_value, path_value))


def e164(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"
    return f"+{digits}"


def parse_inbound(body: dict[str, Any]) -> str | None:
    """Text of an inbound message from the user's own phone, or None for status callbacks,
    empty payloads, or texts from anyone else."""
    if body.get("is_outbound") is True:
        return None
    from_number = body.get("from_number") or body.get("number") or ""
    content = body.get("content") or ""
    if not isinstance(from_number, str) or not isinstance(content, str) or not content.strip():
        return None
    if e164(from_number) != e164(config.MY_PHONE):
        log.warning("Ignoring text from unknown number %s", from_number)
        return None
    return content.strip()
