from app.mail.outlook import _search_literal, html_to_text, parse_message

ME = "Me@Outlook.com"

_MSG = {
    "id": "AAMk1",
    "conversationId": "AAQk9",
    "from": {"emailAddress": {"name": "Alice Smith", "address": "alice@example.com"}},
    "toRecipients": [{"emailAddress": {"name": "Me", "address": "me@outlook.com"}}],
    "ccRecipients": [
        {"emailAddress": {"name": "Bob", "address": "bob@example.com"}},
        {"emailAddress": {"name": "", "address": ""}},
    ],
    "subject": "Lunch?",
    "receivedDateTime": "2026-09-25T14:03:00Z",
    "sentDateTime": "2026-09-25T14:02:00Z",
    "bodyPreview": "Are you free",
    "internetMessageId": "<abc@example.com>",
    "isDraft": False,
    "categories": ["Personal"],
    "body": {
        "contentType": "html",
        "content": "<html><head><style>p{color:red}</style></head><body>"
                   "<p>Are you &amp; Bob free   for <b>lunch</b>?</p><script>x()</script>"
                   "<div>Thursday<br>noon</div></body></html>",
    },
}


def test_maps_html_message() -> None:
    m = parse_message(_MSG, ME)
    assert m.account == "outlook"
    assert m.id == "AAMk1"
    assert m.thread_id == "AAQk9"
    assert m.from_addr == "alice@example.com"
    assert m.from_name == "Alice Smith"
    assert m.to == ["me@outlook.com"]
    assert m.cc == ["bob@example.com"]
    assert m.subject == "Lunch?"
    assert m.date == "2026-09-25T14:03:00Z"
    assert m.snippet == "Are you free"
    assert m.message_id_header == "<abc@example.com>"
    assert m.labels == ["Personal"]
    assert m.is_from_me is False
    assert m.body == "Are you & Bob free for lunch?\n\nThursday\nnoon"


def test_text_body_and_from_me() -> None:
    msg = dict(_MSG)
    msg["from"] = {"emailAddress": {"name": "Me", "address": "ME@outlook.com"}}
    msg["body"] = {"contentType": "text", "content": "<not html> plain"}
    del msg["receivedDateTime"]
    m = parse_message(msg, ME)
    assert m.is_from_me is True
    assert m.body == "<not html> plain"
    assert m.date == "2026-09-25T14:02:00Z"


def test_missing_fields() -> None:
    m = parse_message({"id": "x"}, ME)
    assert (m.from_addr, m.from_name, m.to, m.cc, m.body, m.subject) == ("", "", [], [], "", "")
    assert m.message_id_header is None
    assert m.is_from_me is False


def test_helpers() -> None:
    assert html_to_text("a<p>b</p><p></p>\n\n<p>c</p>") == "a\nb\n\nc"
    assert _search_literal('say "hi"') == '"say \\"hi\\""'
