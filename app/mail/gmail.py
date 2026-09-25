"""Gmail implementation of MailProvider via the Gmail REST API."""
from __future__ import annotations

import base64
import email.utils
from datetime import datetime, timezone
from email.message import EmailMessage as MimeMessage
from html.parser import HTMLParser
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app import config
from app.mail.base import Contact, EmailMessage

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
_HEADERS = ("From", "To", "Cc", "Subject", "Date", "Message-ID")
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}


class _HtmlToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self._parts).splitlines()]
        out: list[str] = []
        for line in lines:
            if line or (out and out[-1]):
                out.append(line)
        return "\n".join(out).strip()


def html_to_text(html: str) -> str:
    p = _HtmlToText()
    p.feed(html)
    p.close()
    return p.text()


def _b64url_decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _collect_bodies(part: dict[str, Any], out: dict[str, list[str]]) -> None:
    mime = part.get("mimeType", "")
    data = part.get("body", {}).get("data")
    if data and mime in ("text/plain", "text/html"):
        out[mime].append(_b64url_decode(data))
    for sub in part.get("parts", []) or []:
        _collect_bodies(sub, out)


def extract_body(payload: dict[str, Any]) -> str:
    """Plain text from a Gmail message payload: text/plain parts if any, else stripped text/html."""
    found: dict[str, list[str]] = {"text/plain": [], "text/html": []}
    _collect_bodies(payload, found)
    if found["text/plain"]:
        return "\n".join(found["text/plain"]).strip()
    if found["text/html"]:
        return html_to_text("\n".join(found["text/html"]))
    return ""


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    wanted = {h.lower(): h for h in _HEADERS}
    out: dict[str, str] = {}
    for h in payload.get("headers", []) or []:
        key = wanted.get(h.get("name", "").lower())
        if key and key not in out:
            out[key] = h.get("value", "")
    return out


def _addresses(header: str) -> list[str]:
    return [addr.lower() for _, addr in email.utils.getaddresses([header]) if addr]


def _iso_date(date_header: str, internal_ms: str | None) -> str:
    if date_header:
        try:
            return email.utils.parsedate_to_datetime(date_header).isoformat()
        except (TypeError, ValueError):
            pass
    if internal_ms:
        return datetime.fromtimestamp(int(internal_ms) / 1000, tz=timezone.utc).isoformat()
    return ""


def parse_message(raw: dict[str, Any], own_address: str) -> EmailMessage:
    payload = raw.get("payload", {}) or {}
    h = _headers(payload)
    from_name, from_addr = email.utils.parseaddr(h.get("From", ""))
    from_addr = from_addr.lower()
    return EmailMessage(
        account="gmail",
        id=raw["id"],
        thread_id=raw.get("threadId", ""),
        from_addr=from_addr,
        from_name=from_name,
        to=_addresses(h.get("To", "")),
        cc=_addresses(h.get("Cc", "")),
        subject=h.get("Subject", ""),
        date=_iso_date(h.get("Date", ""), raw.get("internalDate")),
        body=extract_body(payload),
        snippet=raw.get("snippet", ""),
        message_id_header=h.get("Message-ID") or None,
        is_from_me=bool(from_addr) and from_addr == own_address.lower(),
        labels=list(raw.get("labelIds", []) or []),
    )


def _wrap(err: HttpError, what: str) -> RuntimeError:
    status = getattr(err.resp, "status", "?")
    return RuntimeError(f"Gmail API error during {what} (HTTP {status}): {err.reason or err}")


class GmailProvider:
    name = "gmail"

    def __init__(self) -> None:
        creds = Credentials.from_authorized_user_file(str(config.GMAIL_TOKEN), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            config.GMAIL_TOKEN.write_text(creds.to_json())
        self._svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self._users = self._svc.users()
        try:
            self.address: str = self._users.getProfile(userId="me").execute()["emailAddress"].lower()
        except HttpError as e:
            raise _wrap(e, "getProfile") from e

    def _list_ids(self, query: str, cap: int) -> list[str]:
        ids: list[str] = []
        token: str | None = None
        try:
            while len(ids) < cap:
                resp = self._users.messages().list(
                    userId="me", q=query, maxResults=min(cap - len(ids), 100), pageToken=token
                ).execute()
                ids.extend(m["id"] for m in resp.get("messages", []))
                token = resp.get("nextPageToken")
                if not token:
                    break
        except HttpError as e:
            raise _wrap(e, f"search {query!r}") from e
        return ids[:cap]

    def _fetch(self, ids: list[str], body_chars: int | None = None) -> list[EmailMessage]:
        msgs = [self.get_message(i) for i in ids]
        if body_chars is not None:
            for m in msgs:
                if len(m.body) > body_chars:
                    m.body = m.body[:body_chars] + "…"
        return msgs

    def list_new_inbox_ids(self, since_iso: str) -> list[str]:
        since = datetime.fromisoformat(since_iso)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        return self._list_ids(f"in:inbox -from:me after:{int(since.timestamp())}", cap=50)

    def get_message(self, message_id: str) -> EmailMessage:
        try:
            raw = self._users.messages().get(userId="me", id=message_id, format="full").execute()
        except HttpError as e:
            raise _wrap(e, f"get message {message_id}") from e
        return parse_message(raw, self.address)

    def get_thread(self, thread_id: str) -> list[EmailMessage]:
        try:
            raw = self._users.threads().get(userId="me", id=thread_id, format="full").execute()
        except HttpError as e:
            raise _wrap(e, f"get thread {thread_id}") from e
        msgs = [parse_message(m, self.address) for m in raw.get("messages", [])]
        msgs.sort(key=lambda m: m.date)
        return msgs

    def search(self, query: str, max_results: int = 10) -> list[EmailMessage]:
        return self._fetch(self._list_ids(query, cap=max_results), body_chars=2000)

    def recent_sent(self, n: int = 20) -> list[EmailMessage]:
        return self._fetch(self._list_ids("in:sent", cap=n))

    def find_contacts(self, name_or_fragment: str) -> list[Contact]:
        frag = name_or_fragment.strip()
        contacts: dict[str, Contact] = {}

        def bump(addr: str, name: str, date: str, *, sent: bool) -> None:
            addr = addr.lower()
            if not addr or addr == self.address:
                return
            c = contacts.setdefault(addr, Contact(email=addr))
            if sent:
                c.sent_to_count += 1
            else:
                c.received_count += 1
            if name and not c.name:
                c.name = name
            if date > c.last_seen:
                c.last_seen = date

        for m in self._fetch(self._list_ids(f"in:sent to:{frag}", cap=40)):
            for header_addrs in (m.to, m.cc):
                for addr in header_addrs:
                    bump(addr, "", m.date, sent=True)
        for m in self._fetch(self._list_ids(f"from:{frag}", cap=40)):
            bump(m.from_addr, m.from_name, m.date, sent=False)

        return sorted(contacts.values(), key=lambda c: (-c.sent_to_count, -c.received_count, c.email))

    def send(self, to: list[str], subject: str, body: str, cc: list[str] | None = None,
             reply_to: EmailMessage | None = None) -> str:
        msg = MimeMessage()
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["From"] = self.address
        request: dict[str, Any] = {}
        if reply_to is not None:
            subject = reply_to.subject or subject
            if not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"
            if reply_to.message_id_header:
                msg["In-Reply-To"] = reply_to.message_id_header
                msg["References"] = reply_to.message_id_header
            request["threadId"] = reply_to.thread_id
        msg["Subject"] = subject
        msg.set_content(body)
        request["raw"] = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        try:
            sent = self._users.messages().send(userId="me", body=request).execute()
        except HttpError as e:
            raise _wrap(e, "send") from e
        return sent["id"]
