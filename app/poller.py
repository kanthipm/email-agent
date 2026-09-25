"""Watches every configured inbox; emails that need a reply become drafts texted to the user."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from app import config, llm, sms, store
from app.mail import providers
from app.mail.base import EmailMessage, MailProvider

log = logging.getLogger(__name__)
OVERLAP = timedelta(minutes=10)   # re-scan window; processed_emails dedupes


def _since_key(name: str) -> str:
    return f"poll_since:{name}"


def process_email(p: MailProvider, email: EmailMessage) -> dict | None:
    if email.is_from_me:
        return None
    try:
        thread = p.get_thread(email.thread_id)
    except Exception as e:
        log.warning("thread fetch failed for %s:%s: %s", p.name, email.id, e)
        thread = [email]
    verdict = llm.triage(email, thread)
    log.info("%s:%s from %s -> needs_reply=%s (%s)", p.name, email.id, email.from_addr,
             verdict["needs_reply"], verdict["reason"])
    if not verdict["needs_reply"] or not verdict["reply_body"].strip():
        return None
    subject = email.subject if email.subject.lower().startswith("re:") else f"Re: {email.subject}"
    draft = store.create_draft(account=p.name, to_addrs=[email.from_addr], subject=subject,
                               body=verdict["reply_body"].strip(), source="inbound",
                               reply_to_message_id=email.id, thread_id=email.thread_id,
                               summary=verdict["summary"])
    text = llm.new_email_text(draft, email, verdict["summary"])
    sms.send_text(text)
    store.mark_shown(draft["id"])
    store.append_chat("user", f"[event] New email arrived; you texted the user draft #{draft['id']}.")
    store.append_chat("assistant", text)
    return draft


def poll_once() -> int:
    """Scan every provider once. Returns the number of drafts texted out."""
    sent = 0
    now = datetime.now(timezone.utc)
    for name, p in providers().items():
        since = store.get_kv(_since_key(name))
        if since is None:
            # First run: only look forward, never triage the whole backlog.
            store.set_kv(_since_key(name), now.isoformat(timespec="seconds"))
            continue
        try:
            ids = p.list_new_inbox_ids(since)
        except Exception as e:
            log.error("%s: listing inbox failed: %s", name, e)
            continue
        for mid in reversed(ids):     # oldest first so drafts arrive in order
            if store.is_processed(name, mid):
                continue
            try:
                email = p.get_message(mid)
                if process_email(p, email):
                    sent += 1
            except Exception:
                log.exception("%s: failed processing %s", name, mid)
            store.mark_processed(name, mid)
        store.set_kv(_since_key(name), (now - OVERLAP).isoformat(timespec="seconds"))
    return sent


def run_forever(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            poll_once()
        except Exception:
            log.exception("poll failed")
        stop.wait(config.POLL_INTERVAL_SECONDS)
