"""FastAPI app: Sendblue inbound webhook plus the background poller and SMS worker."""
from __future__ import annotations

import logging
import queue
import threading
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request

from app import config, llm, poller, sms, store
from app.mail import providers

log = logging.getLogger(__name__)
_inbox: queue.Queue[str] = queue.Queue()
_stop = threading.Event()


def _sms_worker() -> None:
    """Serialises agent turns so two texts never interleave the conversation."""
    while not _stop.is_set():
        try:
            text = _inbox.get(timeout=1)
        except queue.Empty:
            continue
        try:
            reply = llm.handle_sms(text)
        except Exception as e:
            log.exception("agent turn failed")
            reply = f"Something went wrong on my end: {e}"
        try:
            sms.send_text(reply)
        except Exception:
            log.exception("could not text reply")


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store.init()
    log.info("mail accounts: %s", list(providers()) or "none")
    threads = [threading.Thread(target=_sms_worker, daemon=True, name="sms-worker"),
               threading.Thread(target=poller.run_forever, args=(_stop,), daemon=True, name="poller")]
    for t in threads:
        t.start()
    yield
    _stop.set()


app = FastAPI(title="email-agent", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "accounts": list(providers()), "sms": sms.configured(),
            "pending_drafts": len(store.pending_drafts())}


def _inbound(request: Request, body: dict[str, Any], path_secret: str | None) -> dict:
    if not config.SENDBLUE_WEBHOOK_SECRET:
        raise HTTPException(503, "SENDBLUE_WEBHOOK_SECRET is not set")
    if not sms.secret_ok(request.headers.get("sb-signing-secret"), path_secret):
        raise HTTPException(401, "bad webhook secret")
    text = sms.parse_inbound(body)
    if text is None:
        return {"handled": False}
    _inbox.put(text)
    return {"handled": True}


@app.post("/webhooks/sendblue")
def inbound_header(request: Request, body: dict[str, Any] = Body(...)) -> dict:
    return _inbound(request, body, None)


@app.post("/webhooks/sendblue/{secret}")
def inbound_path(secret: str, request: Request, body: dict[str, Any] = Body(...)) -> dict:
    return _inbound(request, body, secret)
