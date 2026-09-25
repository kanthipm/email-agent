"""Outlook / Microsoft 365 provider via Microsoft Graph REST."""
from __future__ import annotations

from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote

import httpx
import msal

from app import config
from app.mail.base import Contact, EmailMessage

SCOPES = ["Mail.ReadWrite", "Mail.Send", "User.Read"]
GRAPH = "https://graph.microsoft.com/v1.0"
_SELECT = ("id,conversationId,from,toRecipients,ccRecipients,subject,receivedDateTime,"
           "sentDateTime,body,bodyPreview,internetMessageId,isDraft,categories")
_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    p.close()
    lines = [" ".join(line.split()) for line in "".join(p.parts).splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def _addr(rec: dict[str, Any] | None) -> tuple[str, str]:
    ea = (rec or {}).get("emailAddress") or {}
    return (ea.get("address") or "").strip(), (ea.get("name") or "").strip()


def parse_message(m: dict[str, Any], my_address: str) -> EmailMessage:
    from_addr, from_name = _addr(m.get("from"))
    body = m.get("body") or {}
    content = body.get("content") or ""
    if (body.get("contentType") or "").lower() == "html":
        content = html_to_text(content)
    return EmailMessage(
        account="outlook",
        id=m["id"],
        thread_id=m.get("conversationId") or "",
        from_addr=from_addr,
        from_name=from_name,
        to=[a for a, _ in map(_addr, m.get("toRecipients") or []) if a],
        cc=[a for a, _ in map(_addr, m.get("ccRecipients") or []) if a],
        subject=m.get("subject") or "",
        date=m.get("receivedDateTime") or m.get("sentDateTime") or "",
        body=content,
        snippet=m.get("bodyPreview") or "",
        message_id_header=m.get("internetMessageId"),
        is_from_me=bool(from_addr) and from_addr.lower() == my_address.lower(),
        labels=list(m.get("categories") or []),
    )


def _search_literal(query: str) -> str:
    return '"' + query.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _recipients(addrs: list[str] | None) -> list[dict[str, Any]]:
    return [{"emailAddress": {"address": a}} for a in (addrs or [])]


class OutlookProvider:
    name = "outlook"

    def __init__(self) -> None:
        self._cache = msal.SerializableTokenCache()
        if config.OUTLOOK_TOKEN.exists():
            self._cache.deserialize(config.OUTLOOK_TOKEN.read_text())
        self._app = msal.PublicClientApplication(
            config.OUTLOOK_CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{config.OUTLOOK_TENANT}",
            token_cache=self._cache,
        )
        self._http = httpx.Client(base_url=GRAPH, timeout=30)
        me = self._get("/me")
        self.address: str = me.get("mail") or me.get("userPrincipalName") or ""

    # --- auth / transport ---------------------------------------------------

    def _token(self) -> str:
        accounts = self._app.get_accounts()
        result = self._app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
        if self._cache.has_state_changed:
            config.OUTLOOK_TOKEN.write_text(self._cache.serialize())
        if not result or "access_token" not in result:
            raise RuntimeError("Outlook token missing or expired. Run: python scripts/auth_outlook.py")
        return result["access_token"]

    def _request(self, method: str, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Prefer": 'outlook.body-content-type="text"',
        }
        r = self._http.request(method, path, headers=headers, json=json)
        if r.status_code // 100 != 2:
            raise RuntimeError(f"Graph {method} {path} -> {r.status_code}: {r.text[:500]}")
        return r.json() if r.content else {}

    def _get(self, path: str, **params: str | int) -> dict[str, Any]:
        # Encode by hand so spaces become %20 (not "+"), which Graph's OData parser expects.
        if params:
            path += "?" + "&".join(f"${k}={quote(str(v), safe='')}" for k, v in params.items())
        return self._request("GET", path)

    def _post(self, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("POST", path, json)

    def _patch(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", path, json)

    def _messages(self, path: str, **params: str | int) -> list[EmailMessage]:
        return [parse_message(m, self.address) for m in self._get(path, **params).get("value", [])]

    # --- MailProvider -------------------------------------------------------

    def list_new_inbox_ids(self, since_iso: str) -> list[str]:
        data = self._get(
            "/me/mailFolders/inbox/messages",
            filter=f"receivedDateTime ge {since_iso}",
            orderby="receivedDateTime desc",
            top=50,
            select="id,from",
        )
        me = self.address.lower()
        return [m["id"] for m in data.get("value", []) if _addr(m.get("from"))[0].lower() != me]

    def get_message(self, message_id: str) -> EmailMessage:
        return parse_message(self._get(f"/me/messages/{message_id}", select=_SELECT), self.address)

    def get_thread(self, thread_id: str) -> list[EmailMessage]:
        cid = thread_id.replace("'", "''")
        return self._messages(
            "/me/messages",
            filter=f"conversationId eq '{cid}'",
            orderby="receivedDateTime asc",
            top=50,
            select=_SELECT,
        )

    def search(self, query: str, max_results: int = 10) -> list[EmailMessage]:
        msgs = self._messages("/me/messages", search=_search_literal(query), top=max_results, select=_SELECT)
        for m in msgs:
            if len(m.body) > 2000:
                m.body = m.body[:2000] + "…"
        return msgs

    def recent_sent(self, n: int = 20) -> list[EmailMessage]:
        return self._messages(
            "/me/mailFolders/sentitems/messages", orderby="sentDateTime desc", top=n, select=_SELECT,
        )

    def find_contacts(self, name_or_fragment: str) -> list[Contact]:
        me = self.address.lower()
        found: dict[str, Contact] = {}

        def bump(addr: str, name: str, date: str, field: str) -> None:
            key = addr.lower()
            if not key or key == me:
                return
            c = found.setdefault(key, Contact(email=addr))
            setattr(c, field, getattr(c, field) + 1)
            if name and not c.name:
                c.name = name
            if date > c.last_seen:
                c.last_seen = date

        sent = self._get(
            "/me/mailFolders/sentitems/messages",
            search=_search_literal(name_or_fragment), top=40,
            select="toRecipients,ccRecipients,sentDateTime",
        )
        for m in sent.get("value", []):
            date = m.get("sentDateTime") or ""
            for rec in (m.get("toRecipients") or []) + (m.get("ccRecipients") or []):
                bump(*_addr(rec), date, "sent_to_count")

        received = self._get(
            "/me/messages",
            search=_search_literal(f"from:{name_or_fragment}"), top=40,
            select="from,receivedDateTime",
        )
        for m in received.get("value", []):
            bump(*_addr(m.get("from")), m.get("receivedDateTime") or "", "received_count")

        return sorted(found.values(), key=lambda c: (-c.sent_to_count, -c.received_count, c.email))

    def send(self, to: list[str], subject: str, body: str, cc: list[str] | None = None,
             reply_to: EmailMessage | None = None) -> str:
        if reply_to is None:
            self._post("/me/sendMail", {
                "message": {
                    "subject": subject,
                    "body": {"contentType": "text", "content": body},
                    "toRecipients": _recipients(to),
                    "ccRecipients": _recipients(cc),
                },
                "saveToSentItems": True,
            })
            return "sent"

        draft = self._post(f"/me/messages/{reply_to.id}/createReply")
        draft_id = draft["id"]
        patch: dict[str, Any] = {
            "body": {"contentType": "text", "content": body},
            "toRecipients": _recipients(to),
            "ccRecipients": _recipients(cc),
        }
        if subject and subject.strip().lower() != (draft.get("subject") or "").strip().lower():
            patch["subject"] = subject
        self._patch(f"/me/messages/{draft_id}", patch)
        self._post(f"/me/messages/{draft_id}/send")
        return draft_id
