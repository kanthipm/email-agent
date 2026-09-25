"""Provider-neutral email types. Gmail and Outlook each implement MailProvider."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class EmailMessage:
    account: str                 # "gmail" | "outlook"
    id: str                      # provider message id
    thread_id: str               # gmail threadId / outlook conversationId
    from_addr: str
    from_name: str
    to: list[str]
    cc: list[str]
    subject: str
    date: str                    # ISO 8601
    body: str                    # plain text (html stripped)
    snippet: str = ""
    message_id_header: str | None = None   # RFC 2822 Message-ID, for In-Reply-To
    is_from_me: bool = False
    labels: list[str] = field(default_factory=list)

    def short(self, body_chars: int = 400) -> str:
        who = f"{self.from_name} <{self.from_addr}>" if self.from_name else self.from_addr
        b = self.body.strip().replace("\r", "")
        if len(b) > body_chars:
            b = b[:body_chars] + "…"
        return (f"[{self.account}:{self.id}] {self.date}\nFrom: {who}\nTo: {', '.join(self.to)}\n"
                f"Subject: {self.subject}\n{b}")


@dataclass
class Contact:
    email: str
    name: str = ""
    sent_to_count: int = 0       # how often the user has emailed them
    received_count: int = 0      # how often they emailed the user
    last_seen: str = ""          # ISO date


class MailProvider(Protocol):
    name: str          # "gmail" | "outlook"
    address: str       # the user's own address on this account

    def list_new_inbox_ids(self, since_iso: str) -> list[str]:
        """IDs of inbox messages received after since_iso, not sent by the user."""

    def get_message(self, message_id: str) -> EmailMessage: ...

    def get_thread(self, thread_id: str) -> list[EmailMessage]:
        """All messages in the thread, oldest first."""

    def search(self, query: str, max_results: int = 10) -> list[EmailMessage]:
        """Free-text search across all mail (subject, body, from, to)."""

    def recent_sent(self, n: int = 20) -> list[EmailMessage]:
        """The user's most recent sent messages, newest first (for writing style)."""

    def find_contacts(self, name_or_fragment: str) -> list[Contact]:
        """People matching a name or address fragment, ranked by how often the user emails them."""

    def send(self, to: list[str], subject: str, body: str, cc: list[str] | None = None,
             reply_to: EmailMessage | None = None) -> str:
        """Send plain-text mail. If reply_to is given, thread it (In-Reply-To/References, same subject
        with Re:). Returns the sent message id."""
