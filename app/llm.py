"""LLM layer (Groq, OpenAI-compatible chat API): triage inbound mail, draft replies in the
user's voice, and run the SMS conversation (edit / create / send drafts, look up past mail)."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx

from app import config, store
from app.mail import get as get_provider, providers
from app.mail.base import EmailMessage

log = logging.getLogger(__name__)
BASE_URL = "https://api.groq.com/openai/v1"
MAX_TOOL_ROUNDS = 12
STYLE_TTL_S = 24 * 3600


def _chat(messages: list[dict], *, tools: list[dict] | None = None, json_mode: bool = False,
          max_tokens: int = 4000, temperature: float = 0.4) -> dict:
    """One chat completion. Returns the assistant message dict. Retries once on 429/5xx."""
    if not config.GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set")
    body: dict[str, Any] = {"model": config.GROQ_MODEL, "messages": messages,
                            "max_tokens": max_tokens, "temperature": temperature}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {config.GROQ_API_KEY}"}
    for attempt in range(6):
        r = httpx.post(f"{BASE_URL}/chat/completions", json=body, headers=headers, timeout=90.0)
        if (r.status_code == 429 or r.status_code >= 500) and attempt < 5:
            wait = _retry_after(r)
            log.warning("Groq %s, retrying in %.1fs", r.status_code, wait)
            time.sleep(wait)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Groq {r.status_code}: {r.text[:300]}")
        return r.json()["choices"][0]["message"]
    raise RuntimeError("Groq unavailable")


_WAIT_RE = re.compile(r"try again in (?:(\d+)m)?(?:([\d.]+)s|([\d.]+)ms)")


def _retry_after(r: httpx.Response) -> float:
    """Seconds to wait, from the Retry-After header or Groq's 'try again in 1m2.5s' text."""
    if r.headers.get("retry-after"):
        return min(float(r.headers["retry-after"]), 60.0) + 0.5
    m = _WAIT_RE.search(r.text)
    if m:
        mins, secs, ms = m.groups()
        return min(int(mins or 0) * 60 + float(secs or 0) + float(ms or 0) / 1000, 60.0) + 0.5
    return 5.0


def _me() -> str:
    return config.MY_NAME or "the user"


# ---------------------------------------------------------------- writing style
_QUOTE_RE = re.compile(r"^(On .{5,120} wrote:|From: .*|-----Original Message-----|>).*", re.M | re.S)


def _own_words(body: str) -> str:
    """Strip quoted history so style samples only contain what the user typed."""
    m = _QUOTE_RE.search(body)
    return (body[: m.start()] if m else body).strip()[:800]


def style_samples(force: bool = False) -> str:
    cached = store.get_kv("style_samples")
    if cached and not force and time.time() - cached["at"] < STYLE_TTL_S:
        return cached["text"]
    parts: list[str] = []
    for name, p in providers().items():
        try:
            sent = p.recent_sent(12)
        except Exception as e:  # a dead provider must not block drafting
            log.warning("style samples: %s failed: %s", name, e)
            continue
        for m in sent:
            words = _own_words(m.body)
            if len(words) < 20:
                continue
            parts.append(f"--- ({name}) To: {', '.join(m.to)} | Subject: {m.subject}\n{words}")
    text = "\n\n".join(parts) if parts else "(no sent mail available yet)"
    store.set_kv("style_samples", {"at": time.time(), "text": text})
    return text


def _accounts_line() -> str:
    return ", ".join(f"{n} <{p.address}>" for n, p in providers().items()) or "(none configured)"


def _persona() -> str:
    return (
        f"You are the personal email assistant of {_me()}. You write emails in their voice and manage "
        f"them with them over text message.\n"
        f"Email accounts: {_accounts_line()}\n\n"
        f"## How {_me()} writes (their recent sent emails)\n{style_samples()}\n\n"
        "Match their greeting and sign-off habits, sentence length, formality, and punctuation. "
        "Never invent facts, dates, prices, commitments, or details only they would know; leave a short "
        "bracketed placeholder like [date] instead."
    )


# ---------------------------------------------------------------- triage
def triage(email: EmailMessage, thread: list[EmailMessage]) -> dict:
    """Decide whether the user should reply and, if so, draft the reply."""
    history = "\n\n".join(m.short(1200) for m in thread if m.id != email.id) or "(no earlier messages)"
    prompt = (
        f"A new email arrived in the {email.account} inbox. Decide whether {_me()} personally needs to "
        "reply.\n\n"
        "needs_reply is true when a real person is asking them something, waiting on them, or clearly "
        "expects an acknowledgement. It is false for newsletters, notifications, receipts, marketing, "
        "automated or no-reply mail, mass announcements, FYI/CC-only mail, calendar responses, and "
        f"threads where {_me()} already answered the latest question.\n\n"
        "Answer with only a JSON object with exactly these keys:\n"
        '{"needs_reply": true|false, "reason": "<short>", "summary": "<one sentence for a text message: '
        'who, what they want, any deadline>", "reply_body": "<if needs_reply, the full reply in their voice, '
        'plain text, no subject line, as short as they would write; otherwise an empty string>"}\n\n'
        f"## Earlier messages in this thread (oldest first)\n{history}\n\n"
        f"## New email\n{email.short(6000)}"
    )
    msg = _chat([{"role": "system", "content": _persona()}, {"role": "user", "content": prompt}],
                json_mode=True, temperature=0.3)
    data = json.loads(msg["content"] or "{}")
    return {"needs_reply": bool(data.get("needs_reply")), "reason": str(data.get("reason", "")),
            "summary": str(data.get("summary", "")), "reply_body": str(data.get("reply_body", ""))}


# ---------------------------------------------------------------- SMS agent tools
def _fmt_draft(d: dict) -> str:
    cc = f"\nCc: {', '.join(d['cc_addrs'])}" if d["cc_addrs"] else ""
    kind = "reply" if d["reply_to_message_id"] else "new email"
    return (f"Draft #{d['id']} ({d['account']}, {kind}, v{d['version']})\nTo: {', '.join(d['to_addrs'])}{cc}\n"
            f"Subject: {d['subject']}\n\n{d['body']}")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _tool_list_drafts() -> str:
    ds = store.pending_drafts()
    if not ds:
        return "No pending drafts."
    return "\n".join(f"#{d['id']} [{d['account']}] to {', '.join(d['to_addrs'])} — {d['subject']}"
                     f"{' — ' + d['summary'] if d['summary'] else ''}" for d in ds)


def _tool_get_draft(draft_id: int) -> str:
    d = store.get_draft(draft_id)
    if not d:
        return f"Error: no draft #{draft_id}."
    return _fmt_draft(d) + f"\n\nstatus: {d['status']}"


def _tool_update_draft(draft_id: int, body: str | None = None, subject: str | None = None,
                       to: list[str] | None = None, cc: list[str] | None = None) -> str:
    d = store.get_draft(draft_id)
    if not d or d["status"] != "pending":
        return f"Error: draft #{draft_id} is not pending."
    fields: dict[str, Any] = {}
    if body is not None:
        fields["body"] = body
    if subject is not None:
        fields["subject"] = subject
    if to is not None:
        fields["to_addrs"] = to
    if cc is not None:
        fields["cc_addrs"] = cc
    if not fields:
        return "Error: nothing to update."
    d = store.update_draft(draft_id, **fields)
    return "Updated. Show the user the full new draft verbatim; it cannot be sent until they have seen it.\n\n" + _fmt_draft(d)


def _tool_create_draft(account: str, to: list[str], subject: str, body: str, cc: list[str] | None = None,
                       reply_to_email_id: str | None = None) -> str:
    try:
        p = get_provider(account)
    except KeyError as e:
        return f"Error: {e}"
    reply_to = thread_id = None
    if reply_to_email_id:
        try:
            original = p.get_message(reply_to_email_id)
        except Exception as e:
            return f"Error reading email {reply_to_email_id}: {e}"
        reply_to, thread_id = original.id, original.thread_id
        if not subject:
            subject = original.subject if original.subject.lower().startswith("re:") else f"Re: {original.subject}"
    d = store.create_draft(account=account, to_addrs=to, cc_addrs=cc or [], subject=subject, body=body,
                           source="user", reply_to_message_id=reply_to, thread_id=thread_id)
    return "Created. Show the user the full draft verbatim and ask them to confirm before sending.\n\n" + _fmt_draft(d)


def _tool_send_draft(draft_id: int) -> str:
    d = store.get_draft(draft_id)
    if not d or d["status"] != "pending":
        return f"Error: draft #{draft_id} is not pending."
    if d["shown_version"] != d["version"]:
        return ("Error: the user has not seen the current version of this draft. Reply with the full draft "
                "text verbatim and ask them to confirm; send only after they approve that exact text.")
    p = get_provider(d["account"])
    reply_to = p.get_message(d["reply_to_message_id"]) if d["reply_to_message_id"] else None
    try:
        sent_id = p.send(d["to_addrs"], d["subject"], d["body"], cc=d["cc_addrs"], reply_to=reply_to)
    except Exception as e:
        log.exception("send failed")
        return f"Error: sending failed: {e}"
    store.mark_sent(draft_id, sent_id)
    return f"Sent draft #{draft_id} to {', '.join(d['to_addrs'])}."


def _tool_discard_draft(draft_id: int) -> str:
    d = store.get_draft(draft_id)
    if not d or d["status"] != "pending":
        return f"Error: draft #{draft_id} is not pending."
    store.discard_draft(draft_id)
    return f"Discarded draft #{draft_id}."


def _tool_search_emails(query: str, account: str | None = None, max_results: int = 8) -> str:
    out = []
    for name, p in providers().items():
        if account and name != account:
            continue
        try:
            for m in p.search(query, max_results=max_results):
                out.append(m.short(500))
        except Exception as e:
            out.append(f"({name} search failed: {e})")
    return "\n\n".join(out) or "No matches."


def _tool_read_email(account: str, email_id: str) -> str:
    try:
        p = get_provider(account)
        m = p.get_message(email_id)
    except Exception as e:
        return f"Error: {e}"
    try:
        thread = [t for t in p.get_thread(m.thread_id) if t.id != m.id]
    except Exception:
        thread = []
    ctx = "\n\n".join(t.short(800) for t in thread)
    return m.short(8000) + (f"\n\n## Rest of thread\n{ctx}" if ctx else "")


def _tool_find_contact(name: str) -> str:
    rows = []
    for acct, p in providers().items():
        try:
            for c in p.find_contacts(name)[:6]:
                rows.append((c.sent_to_count, c.received_count, acct, c))
        except Exception as e:
            log.warning("find_contacts on %s failed: %s", acct, e)
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    lines = [f"[{acct}] {c.name or '?'} <{c.email}> — you emailed them {s}x, they emailed you {r}x, last {c.last_seen}"
             for s, r, acct, c in rows]
    return "\n".join(lines) or f"No one matching '{name}' in past mail."


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required}}}


_STR_LIST = {"type": "array", "items": {"type": "string"}}
_ACCOUNT = {"type": "string", "enum": ["gmail", "outlook"]}

TOOLS: list[dict] = [
    _fn("list_drafts", "List drafts waiting for the user's approval.", {}, []),
    _fn("get_draft", "Full contents of one draft.", {"draft_id": {"type": "integer"}}, ["draft_id"]),
    _fn("update_draft", "Rewrite parts of a pending draft. Pass the complete new body, not a diff.",
        {"draft_id": {"type": "integer"}, "body": {"type": "string"}, "subject": {"type": "string"},
         "to": _STR_LIST, "cc": _STR_LIST}, ["draft_id"]),
    _fn("create_draft",
        "Start a new email or a reply. For a reply pass reply_to_email_id (the provider id from "
        "search_emails/read_email) and leave subject empty to reuse the thread subject.",
        {"account": _ACCOUNT, "to": _STR_LIST, "subject": {"type": "string"}, "body": {"type": "string"},
         "cc": _STR_LIST, "reply_to_email_id": {"type": "string"}}, ["account", "to", "subject", "body"]),
    _fn("send_draft", "Send a draft. Only after the user explicitly approved the exact text they were shown.",
        {"draft_id": {"type": "integer"}}, ["draft_id"]),
    _fn("discard_draft", "Throw a pending draft away.", {"draft_id": {"type": "integer"}}, ["draft_id"]),
    _fn("search_emails",
        "Search past mail. Gmail search syntax works (from:, to:, subject:, newer_than:7d); plain words work "
        "on both accounts. Omit account to search all.",
        {"query": {"type": "string"}, "account": _ACCOUNT, "max_results": {"type": "integer"}}, ["query"]),
    _fn("read_email", "Read one email in full, with the rest of its thread.",
        {"account": _ACCOUNT, "email_id": {"type": "string"}}, ["account", "email_id"]),
    _fn("find_contact",
        "Resolve a person's name or partial address to email addresses, ranked by how often the user emails them.",
        {"name": {"type": "string"}}, ["name"]),
]

_HANDLERS = {
    "list_drafts": _tool_list_drafts, "get_draft": _tool_get_draft, "update_draft": _tool_update_draft,
    "create_draft": _tool_create_draft, "send_draft": _tool_send_draft, "discard_draft": _tool_discard_draft,
    "search_emails": _tool_search_emails, "read_email": _tool_read_email, "find_contact": _tool_find_contact,
}

AGENT_RULES = """
## Working over text message
- You are talking to the user by SMS. Be brief: a sentence or two plus the draft when one changed. Plain text only: no markdown, no **bold**, no bullet symbols; use short lines instead.
- Drafts live in a store and are referenced as #id. "Send", "send it", "looks good, go", "yes" after a draft was shown means send_draft on the draft under discussion (usually the most recent one you showed).
- Never send anything the user has not approved. When you create or change a draft, paste the FULL draft text verbatim in your reply and ask them to confirm. If they ask for changes and to send in the same message, make the changes, show the result, and ask for confirmation instead of sending.
- After a change, show only the updated draft, not a summary of the edit.
- When they name a person ("email Sarah about the invoice"), use find_contact. One clear best match (the address they email most): use it and mention it in your reply. Several plausible matches: ask which, listing the top options.
- When they refer to an email ("reply to the thing from the landlord", "that email about the lease"), use search_emails, then read_email, then create_draft with reply_to_email_id.
- When context is missing, search past mail before asking the user.
- Write in the user's voice from the style samples. Plain text emails, no markdown.
- Never expose tool names or ids other than draft #ids to the user.
"""


def _context_block() -> str:
    return f"\n\n<context>\nPending drafts:\n{_tool_list_drafts()}\nAccounts: {_accounts_line()}\n</context>"


def _mark_shown_drafts(reply: str) -> None:
    n = _norm(reply)
    for d in store.pending_drafts():
        if d["shown_version"] != d["version"] and _norm(d["body"]) in n:
            store.mark_shown(d["id"])


def handle_sms(user_text: str) -> str:
    """One conversational turn: returns the text to send back to the user."""
    history = store.recent_chat()
    store.append_chat({"role": "user", "content": user_text})
    messages: list[dict] = [{"role": "system", "content": _persona() + "\n" + AGENT_RULES}]
    messages += history + [{"role": "user", "content": user_text + _context_block()}]
    reply = ""
    for _ in range(MAX_TOOL_ROUNDS):
        msg = _chat(messages, tools=TOOLS, max_tokens=4000)
        assistant = {"role": "assistant", "content": msg.get("content") or ""}
        calls = msg.get("tool_calls") or []
        if calls:
            assistant["tool_calls"] = calls
        messages.append(assistant)
        store.append_chat(assistant)
        reply = assistant["content"].strip()
        if not calls:
            break
        for c in calls:
            name = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
                out = _HANDLERS[name](**args)
            except Exception as e:
                log.exception("tool %s failed", name)
                out = f"Error: {e}"
            result = {"role": "tool", "tool_call_id": c["id"], "content": out}
            messages.append(result)
            store.append_chat(result)
    else:
        reply = reply or "I got stuck on that. Can you rephrase?"
    if not reply:
        reply = "Done."
    _mark_shown_drafts(reply)
    return reply


def new_email_text(draft: dict, email: EmailMessage, summary: str) -> str:
    who = f"{email.from_name} <{email.from_addr}>" if email.from_name else email.from_addr
    return (f"New email ({email.account}) from {who}\nSubject: {email.subject}\n{summary}\n\n"
            f"{_fmt_draft(draft)}\n\nReply \"send\" to send it, or tell me what to change.")
