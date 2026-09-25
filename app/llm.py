"""Claude layer: triage inbound mail, draft replies in the user's voice, and run the
SMS conversation (edit / create / send drafts, look things up in past mail)."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import anthropic

from app import config, store
from app.mail import get as get_provider, providers
from app.mail.base import EmailMessage

log = logging.getLogger(__name__)
client = anthropic.Anthropic()
BETAS = ["server-side-fallback-2026-07-01"]
MAX_TOOL_ROUNDS = 12
STYLE_TTL_S = 24 * 3600


def _call(**kw: Any) -> anthropic.types.beta.BetaMessage:
    resp = client.beta.messages.create(model=config.CLAUDE_MODEL, betas=BETAS, fallbacks="default", **kw)
    if resp.stop_reason == "refusal":
        why = resp.stop_details.explanation if resp.stop_details else ""
        raise RuntimeError(f"Claude declined the request: {why}")
    return resp


def _text(resp: anthropic.types.beta.BetaMessage) -> str:
    return "".join(b.text for b in resp.content if b.type == "text").strip()


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


def _persona() -> list[dict]:
    """Stable system prompt (cached): who the user is and how they write."""
    text = (
        f"You are the personal email assistant of {_me()}. You write emails in their voice and manage "
        f"them with them over text message.\n"
        f"Email accounts: {_accounts_line()}\n\n"
        f"## How {_me()} writes (their recent sent emails)\n{style_samples()}\n\n"
        "Match their greeting and sign-off habits, sentence length, formality, and punctuation. "
        "Never invent facts, dates, prices, commitments, or details only they would know; leave a short "
        "bracketed placeholder like [date] instead."
    )
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


# ---------------------------------------------------------------- triage
TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "needs_reply": {"type": "boolean"},
        "reason": {"type": "string"},
        "summary": {"type": "string"},
        "reply_body": {"type": "string"},
    },
    "required": ["needs_reply", "reason", "summary", "reply_body"],
    "additionalProperties": False,
}


def triage(email: EmailMessage, thread: list[EmailMessage]) -> dict:
    """Decide whether the user should reply and, if so, draft the reply."""
    history = "\n\n".join(m.short(1200) for m in thread if m.id != email.id) or "(no earlier messages)"
    prompt = (
        f"A new email arrived in the {email.account} inbox. Decide whether {_me()} personally needs to "
        "reply.\n\n"
        "needs_reply is true when a real person is asking them something, waiting on them, or clearly "
        "expects an acknowledgement. It is false for newsletters, notifications, receipts, marketing, "
        "automated or no-reply mail, mass announcements, FYI/CC-only mail, calendar responses, and "
        f"threads where {_me()} already answered the latest question.\n"
        "summary: one sentence for a text message: who, what they want, any deadline.\n"
        "reply_body: if needs_reply, the full reply in their voice (plain text, no subject line); "
        "keep it as short as they would. Otherwise an empty string.\n\n"
        f"## Earlier messages in this thread (oldest first)\n{history}\n\n"
        f"## New email\n{email.short(6000)}"
    )
    resp = _call(
        max_tokens=4000,
        system=_persona(),
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": TRIAGE_SCHEMA}},
    )
    return json.loads(_text(resp))


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
            rows.append((0, 0, acct, None))
            log.warning("find_contacts on %s failed: %s", acct, e)
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    lines = [f"[{acct}] {c.name or '?'} <{c.email}> — you emailed them {s}x, they emailed you {r}x, last {c.last_seen}"
             for s, r, acct, c in rows if c]
    return "\n".join(lines) or f"No one matching '{name}' in past mail."


TOOLS: list[dict] = [
    {"name": "list_drafts", "description": "List drafts waiting for the user's approval.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_draft", "description": "Full contents of one draft.",
     "input_schema": {"type": "object", "properties": {"draft_id": {"type": "integer"}},
                      "required": ["draft_id"], "additionalProperties": False}},
    {"name": "update_draft",
     "description": "Rewrite parts of a pending draft. Pass the complete new body, not a diff.",
     "input_schema": {"type": "object", "properties": {
         "draft_id": {"type": "integer"}, "body": {"type": "string"}, "subject": {"type": "string"},
         "to": {"type": "array", "items": {"type": "string"}}, "cc": {"type": "array", "items": {"type": "string"}}},
         "required": ["draft_id"], "additionalProperties": False}},
    {"name": "create_draft",
     "description": "Start a new email or a reply. For a reply pass reply_to_email_id (the provider id from "
                    "search_emails/read_email) and leave subject empty to reuse the thread subject.",
     "input_schema": {"type": "object", "properties": {
         "account": {"type": "string", "enum": ["gmail", "outlook"]},
         "to": {"type": "array", "items": {"type": "string"}}, "subject": {"type": "string"},
         "body": {"type": "string"}, "cc": {"type": "array", "items": {"type": "string"}},
         "reply_to_email_id": {"type": "string"}},
         "required": ["account", "to", "subject", "body"], "additionalProperties": False}},
    {"name": "send_draft",
     "description": "Send a draft. Only after the user explicitly approved the exact text they were shown.",
     "input_schema": {"type": "object", "properties": {"draft_id": {"type": "integer"}},
                      "required": ["draft_id"], "additionalProperties": False}},
    {"name": "discard_draft", "description": "Throw a pending draft away.",
     "input_schema": {"type": "object", "properties": {"draft_id": {"type": "integer"}},
                      "required": ["draft_id"], "additionalProperties": False}},
    {"name": "search_emails",
     "description": "Search past mail. Gmail search syntax works (from:, to:, subject:, newer_than:7d); plain "
                    "words work on both accounts. Omit account to search all.",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string"}, "account": {"type": "string", "enum": ["gmail", "outlook"]},
         "max_results": {"type": "integer"}}, "required": ["query"], "additionalProperties": False}},
    {"name": "read_email", "description": "Read one email in full, with the rest of its thread.",
     "input_schema": {"type": "object", "properties": {"account": {"type": "string"}, "email_id": {"type": "string"}},
                      "required": ["account", "email_id"], "additionalProperties": False}},
    {"name": "find_contact",
     "description": "Resolve a person's name or partial address to email addresses, ranked by how often the user "
                    "emails them.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}},
                      "required": ["name"], "additionalProperties": False}},
]

_HANDLERS = {
    "list_drafts": _tool_list_drafts, "get_draft": _tool_get_draft, "update_draft": _tool_update_draft,
    "create_draft": _tool_create_draft, "send_draft": _tool_send_draft, "discard_draft": _tool_discard_draft,
    "search_emails": _tool_search_emails, "read_email": _tool_read_email, "find_contact": _tool_find_contact,
}

AGENT_RULES = """
## Working over text message
- You are talking to the user by SMS. Be brief: a sentence or two plus the draft when one changed.
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
    return f"<context>\nPending drafts:\n{_tool_list_drafts()}\nAccounts: {_accounts_line()}\n</context>"


def _keep(block: Any) -> bool:
    return block.type in ("text", "thinking", "redacted_thinking", "tool_use")


def _mark_shown_drafts(reply: str) -> None:
    n = _norm(reply)
    for d in store.pending_drafts():
        if d["shown_version"] != d["version"] and _norm(d["body"]) in n:
            store.mark_shown(d["id"])


def handle_sms(user_text: str) -> str:
    """One conversational turn: returns the text to send back to the user."""
    history = store.recent_chat()
    store.append_chat("user", user_text)
    messages: list[dict] = history + [{"role": "user", "content": [
        {"type": "text", "text": user_text},
        {"type": "text", "text": _context_block()},
    ]}]
    system = _persona() + [{"type": "text", "text": AGENT_RULES}]
    reply = ""
    for _ in range(MAX_TOOL_ROUNDS):
        resp = _call(max_tokens=8000, system=system, tools=TOOLS, messages=messages,
                     output_config={"effort": "medium"})
        content = [b.model_dump(exclude_none=True) for b in resp.content if _keep(b)]
        messages.append({"role": "assistant", "content": content})
        store.append_chat("assistant", content)
        reply = _text(resp)
        calls = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason != "tool_use" or not calls:
            break
        results = []
        for c in calls:
            try:
                out = _HANDLERS[c.name](**c.input)
            except Exception as e:
                log.exception("tool %s failed", c.name)
                out = f"Error: {e}"
            results.append({"type": "tool_result", "tool_use_id": c.id, "content": out,
                            "is_error": out.startswith("Error")})
        messages.append({"role": "user", "content": results})
        store.append_chat("user", results)
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
