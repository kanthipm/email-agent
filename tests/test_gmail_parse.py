import base64

from app.mail.gmail import extract_body, html_to_text, parse_message


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _part(mime: str, text: str) -> dict:
    return {"mimeType": mime, "body": {"data": _b64(text)}}


HTML = "<html><head><style>p{}</style></head><body><p>Hello <b>world</b></p><p>Second&nbsp;line</p></body></html>"


def test_prefers_plain_over_html():
    payload = {
        "mimeType": "multipart/alternative",
        "headers": [],
        "parts": [_part("text/plain", "plain version"), _part("text/html", HTML)],
    }
    assert extract_body(payload) == "plain version"


def test_html_only_is_stripped():
    payload = {"mimeType": "multipart/alternative", "headers": [], "parts": [_part("text/html", HTML)]}
    assert extract_body(payload) == "Hello world\n\nSecond line"


def test_html_to_text_skips_script_and_collapses_blank_lines():
    assert html_to_text("<div>a</div><script>x()</script><div></div><div></div><div>b</div>") == "a\n\nb"


def test_parse_message_headers_and_flags():
    raw = {
        "id": "m1",
        "threadId": "t1",
        "snippet": "hi there",
        "internalDate": "1700000000000",
        "labelIds": ["INBOX", "UNREAD"],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "Jane Doe <Jane@Example.com>"},
                {"name": "To", "value": "me@example.com, Bob <bob@example.com>"},
                {"name": "Subject", "value": "Test"},
                {"name": "Date", "value": "Tue, 14 Nov 2023 22:13:20 +0000"},
                {"name": "Message-ID", "value": "<abc@example.com>"},
            ],
            "body": {"data": _b64("body text")},
        },
    }
    m = parse_message(raw, "ME@example.com")
    assert m.from_addr == "jane@example.com" and m.from_name == "Jane Doe"
    assert m.to == ["me@example.com", "bob@example.com"]
    assert m.date == "2023-11-14T22:13:20+00:00"
    assert m.message_id_header == "<abc@example.com>"
    assert m.labels == ["INBOX", "UNREAD"] and m.snippet == "hi there"
    assert m.body == "body text" and not m.is_from_me

    assert parse_message({**raw, "payload": {**raw["payload"], "headers": [{"name": "From", "value": "me@example.com"}]}},
                         "ME@example.com").is_from_me


def test_date_falls_back_to_internal_date():
    raw = {"id": "m2", "payload": {"headers": [], "body": {}}, "internalDate": "1700000000000"}
    assert parse_message(raw, "me@example.com").date == "2023-11-14T22:13:20+00:00"
