"""SQLite state: processed emails, drafts awaiting approval, and the SMS conversation."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from app.config import DB_PATH

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_emails (
    account TEXT NOT NULL, message_id TEXT NOT NULL, processed_at TEXT NOT NULL,
    PRIMARY KEY (account, message_id));
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    reply_to_message_id TEXT,
    thread_id TEXT,
    to_addrs TEXT NOT NULL,
    cc_addrs TEXT NOT NULL DEFAULT '[]',
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    version INTEGER NOT NULL DEFAULT 1,
    shown_version INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    sent_message_id TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
# drafts.status: pending | sent | discarded.  drafts.source: inbound | user.
# drafts.shown_version: last version the user saw over SMS; send requires shown_version == version.
# chat.content: JSON, either a string or a list of Claude content blocks.


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init() -> None:
    with conn() as c:
        c.executescript(SCHEMA)


@contextmanager
def conn():
    with _lock:
        c = sqlite3.connect(DB_PATH, check_same_thread=False)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


# ---- kv ----
def get_kv(key: str, default: Any = None) -> Any:
    with conn() as c:
        row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_kv(key: str, value: Any) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, json.dumps(value)))


# ---- processed emails ----
def is_processed(account: str, message_id: str) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM processed_emails WHERE account=? AND message_id=?",
                         (account, message_id)).fetchone() is not None


def mark_processed(account: str, message_id: str) -> None:
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO processed_emails VALUES(?,?,?)", (account, message_id, now_iso()))


# ---- drafts ----
def _row_to_draft(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["to_addrs"] = json.loads(d["to_addrs"])
    d["cc_addrs"] = json.loads(d["cc_addrs"])
    return d


def create_draft(*, account: str, to_addrs: list[str], subject: str, body: str, source: str,
                 cc_addrs: list[str] | None = None, reply_to_message_id: str | None = None,
                 thread_id: str | None = None, summary: str = "") -> dict:
    ts = now_iso()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO drafts(account,reply_to_message_id,thread_id,to_addrs,cc_addrs,subject,body,"
            "source,summary,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (account, reply_to_message_id, thread_id, json.dumps(to_addrs), json.dumps(cc_addrs or []),
             subject, body, source, summary, ts, ts))
        return get_draft(cur.lastrowid, c)


def get_draft(draft_id: int, c: sqlite3.Connection | None = None) -> dict | None:
    if c is not None:
        r = c.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        return _row_to_draft(r) if r else None
    with conn() as c2:
        return get_draft(draft_id, c2)


def pending_drafts() -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM drafts WHERE status='pending' ORDER BY id").fetchall()
    return [_row_to_draft(r) for r in rows]


def update_draft(draft_id: int, **fields: Any) -> dict:
    """Edit a pending draft. Any content change bumps version so it must be re-shown before sending."""
    content_keys = {"to_addrs", "cc_addrs", "subject", "body"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in ("to_addrs", "cc_addrs"):
            v = json.dumps(v)
        sets.append(f"{k}=?")
        vals.append(v)
    if content_keys & fields.keys():
        sets.append("version=version+1")
    sets.append("updated_at=?")
    vals.append(now_iso())
    with conn() as c:
        c.execute(f"UPDATE drafts SET {', '.join(sets)} WHERE id=?", (*vals, draft_id))
        return get_draft(draft_id, c)


def mark_shown(draft_id: int) -> None:
    with conn() as c:
        c.execute("UPDATE drafts SET shown_version=version WHERE id=?", (draft_id,))


def mark_sent(draft_id: int, sent_message_id: str) -> None:
    with conn() as c:
        c.execute("UPDATE drafts SET status='sent', sent_message_id=?, updated_at=? WHERE id=?",
                  (sent_message_id, now_iso(), draft_id))


def discard_draft(draft_id: int) -> None:
    with conn() as c:
        c.execute("UPDATE drafts SET status='discarded', updated_at=? WHERE id=?", (now_iso(), draft_id))


# ---- chat history (the SMS conversation with the agent) ----
def append_chat(message: dict) -> None:
    """Store one chat-completion message (user / assistant / tool) verbatim."""
    with conn() as c:
        c.execute("INSERT INTO chat(role,content,created_at) VALUES(?,?,?)",
                  (message["role"], json.dumps(message), now_iso()))


def recent_chat(turns: int = 12) -> list[dict]:
    """The last `turns` user turns and everything after them, oldest first. Always starts at a
    user message, never inside a tool-call / tool-result pair."""
    with conn() as c:
        starts = c.execute("SELECT id FROM chat WHERE role='user' ORDER BY id DESC LIMIT ?",
                           (turns,)).fetchall()
        if not starts:
            return []
        rows = c.execute("SELECT content FROM chat WHERE id>=? ORDER BY id", (starts[-1]["id"],)).fetchall()
    return [json.loads(r["content"]) for r in rows]


def clear_chat() -> None:
    with conn() as c:
        c.execute("DELETE FROM chat")
