"""Approval flow with Claude and the mail provider stubbed out: a draft can only be sent
after the exact current version has been shown to the user."""
import os
import sys
from pathlib import Path
import json

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DB_PATH"] = "test_agent_flow.db"

from app import config  # noqa: E402

config.DB_PATH = config.ROOT / "test_agent_flow.db"
if config.DB_PATH.exists():
    config.DB_PATH.unlink()

from app import llm, store  # noqa: E402
from app import mail  # noqa: E402
from app.mail.base import Contact, EmailMessage  # noqa: E402


class FakeProvider:
    name = "gmail"
    address = "me@example.com"
    sent: list[tuple] = []

    def _msg(self, i="m1"):
        return EmailMessage(account="gmail", id=i, thread_id="t1", from_addr="jane@x.com", from_name="Jane",
                            to=[self.address], cc=[], subject="Lunch?", date="2026-09-25T10:00:00+00:00",
                            body="Want to grab lunch Thursday?", message_id_header="<abc@x.com>")

    def list_new_inbox_ids(self, since_iso): return ["m1"]
    def get_message(self, message_id): return self._msg(message_id)
    def get_thread(self, thread_id): return [self._msg()]
    def search(self, query, max_results=10): return [self._msg()]
    def recent_sent(self, n=20): return []
    def find_contacts(self, frag): return [Contact(email="jane@x.com", name="Jane", sent_to_count=5)]

    def send(self, to, subject, body, cc=None, reply_to=None):
        self.sent.append((to, subject, body))
        return "sent-1"


def text_resp(text):
    return {"role": "assistant", "content": text}


def tool_resp(name, inp, text=""):
    return {"role": "assistant", "content": text,
            "tool_calls": [{"id": "call1", "type": "function",
                            "function": {"name": name, "arguments": json.dumps(inp)}}]}


def run(script):
    """script: list of assistant messages Groq would return, in order."""
    it = iter(script)
    llm._chat = lambda messages, **kw: next(it)
    return llm.handle_sms("hi")


def test_flow():
    store.init()
    fake = FakeProvider()
    mail._providers = {"gmail": fake}
    store.set_kv("style_samples", {"at": 9e12, "text": "sample"})

    d = store.create_draft(account="gmail", to_addrs=["jane@x.com"], subject="Re: Lunch?", body="Sure, Thursday works.",
                           source="inbound", reply_to_message_id="m1", thread_id="t1")
    store.mark_shown(d["id"])

    # 1. user asks for a change and to send at once -> update bumps version, send is refused
    out = run([
        tool_resp("update_draft", {"draft_id": d["id"], "body": "Yes! Thursday at noon?"}),
        tool_resp("send_draft", {"draft_id": d["id"]}),
        text_resp("Updated:\n\nYes! Thursday at noon?\n\nSend it?"),
    ])
    assert fake.sent == [], "must not send an unseen version"
    d2 = store.get_draft(d["id"])
    assert d2["version"] == 2 and d2["shown_version"] == 2, d2   # reply contained the body -> marked shown

    # 2. user approves -> send goes through
    out = run([
        tool_resp("send_draft", {"draft_id": d["id"]}),
        text_resp("Sent."),
    ])
    assert out == "Sent."
    assert fake.sent == [(["jane@x.com"], "Re: Lunch?", "Yes! Thursday at noon?")]
    assert store.get_draft(d["id"])["status"] == "sent"

    # 3. a fresh email via find_contact + create_draft is not sendable until shown
    out = run([
        tool_resp("find_contact", {"name": "jane"}),
        tool_resp("create_draft", {"account": "gmail", "to": ["jane@x.com"], "subject": "Hey", "body": "Quick q."}),
        tool_resp("send_draft", {"draft_id": d["id"] + 1}),
        text_resp("Here it is: Quick q. — ok to send?"),
    ])
    assert len(fake.sent) == 1
    assert store.get_draft(d["id"] + 1)["shown_version"] == 1

    # history window never starts mid tool-call
    hist = store.recent_chat(2)
    assert hist and hist[0]["role"] == "user" and isinstance(hist[0]["content"], str)
    assert all(m["role"] in ("user", "assistant", "tool") for m in hist)


def test_quiet_hours():
    from datetime import datetime
    from app import poller
    assert poller.quiet_hours(datetime(2026, 9, 25, 23, 0))
    assert poller.quiet_hours(datetime(2026, 9, 25, 3, 0))
    assert not poller.quiet_hours(datetime(2026, 9, 25, 7, 0))
    assert not poller.quiet_hours(datetime(2026, 9, 25, 21, 59))
