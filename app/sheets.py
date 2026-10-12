"""Google Sheet mirror of the trackers. The Opportunities tab is two-way: the agent writes the
facts, the user edits Status / Notes, and those come back into the digest (applied, skip and
done hide an entry). The Outreach tab is a one-way snapshot of the pipeline."""
from __future__ import annotations

import logging

from googleapiclient.discovery import build

from app import config, store
from app.mail.gmail import SCOPES, _run

log = logging.getLogger(__name__)
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
TITLE = "Job search tracker"
OPP_HEADERS = ["Name", "Org", "Kind", "For", "Deadline", "Amount", "Eligibility", "URL", "First seen", "Status", "Notes"]
OUT_HEADERS = ["Company", "Role", "Kind", "Contact", "Title", "Email", "LinkedIn", "Stage", "Hiring", "Draft #",
               "Sent", "Follow-ups", "Posting"]
HIDE_STATUSES = {"applied", "skip", "skipped", "done", "no", "ignore", "closed"}


def _service():
    from app.mail import providers
    gmail = providers().get("gmail")
    if gmail is None:
        raise RuntimeError("Gmail is not configured")
    creds = gmail._creds
    if SHEETS_SCOPE not in (creds.scopes or []):
        raise RuntimeError("Google token lacks the Sheets scope. Run: python scripts/auth_gmail.py")
    return build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()


def sheet_url() -> str:
    sid = store.get_kv("sheet_id")
    return f"https://docs.google.com/spreadsheets/d/{sid}" if sid else ""


def _ensure(svc) -> str:
    sid = store.get_kv("sheet_id")
    if sid:
        return sid
    body = {"properties": {"title": TITLE},
            "sheets": [{"properties": {"title": "Opportunities"}}, {"properties": {"title": "Outreach"}}]}
    created = _run(svc.create(body=body, fields="spreadsheetId"))
    sid = created["spreadsheetId"]
    _run(svc.values().update(spreadsheetId=sid, range="Opportunities!A1", valueInputOption="RAW",
                             body={"values": [OPP_HEADERS]}))
    _run(svc.values().update(spreadsheetId=sid, range="Outreach!A1", valueInputOption="RAW",
                             body={"values": [OUT_HEADERS]}))
    store.set_kv("sheet_id", sid)
    log.info("created tracker sheet %s", sheet_url())
    return sid


def sync() -> str:
    """Push both tabs, pull Status/Notes edits back. Returns the sheet URL."""
    svc = _service()
    sid = _ensure(svc)
    _sync_opportunities(svc, sid)
    _sync_outreach(svc, sid)
    return sheet_url()


def _sync_opportunities(svc, sid: str) -> None:
    got = _run(svc.values().get(spreadsheetId=sid, range="Opportunities!A2:K")).get("values", [])
    by_url: dict[str, tuple[int, str, str]] = {}
    for i, row in enumerate(got, start=2):
        row += [""] * (len(OPP_HEADERS) - len(row))
        if row[7]:
            by_url[row[7]] = (i, row[9].strip(), row[10].strip())
    updates, appends = [], []
    for o in store.opportunities():
        facts = [o["name"], o["org"], o["kind"], o["for"], o["deadline"], o["amount"], o["eligibility"], o["url"],
                 o["first_seen"][:10]]
        if o["url"] in by_url:
            rownum, status, notes = by_url[o["url"]]
            updates.append({"range": f"Opportunities!A{rownum}:I{rownum}", "values": [facts]})
            if (status, notes) != (o.get("user_status", ""), o.get("user_notes", "")):
                store.set_opportunity_user(o["url"], status, notes)
        else:
            appends.append(facts + [o.get("user_status", ""), o.get("user_notes", "")])
    if updates:
        _run(svc.values().batchUpdate(spreadsheetId=sid, body={"valueInputOption": "RAW", "data": updates}))
    if appends:
        _run(svc.values().append(spreadsheetId=sid, range="Opportunities!A1", valueInputOption="RAW",
                                 insertDataOption="INSERT_ROWS", body={"values": appends}))


def _sync_outreach(svc, sid: str) -> None:
    rows = [[r["company"], r["role_title"], r["kind"], r["contact_name"], r["contact_title"], r["contact_email"],
             r["contact_linkedin"], r["stage"], r["hiring"], r["draft_id"], (r["sent_at"] or "")[:10],
             r["followups_sent"], r["job_url"]] for r in store.outreach_rows()]
    _run(svc.values().clear(spreadsheetId=sid, range="Outreach!A2:M"))
    if rows:
        _run(svc.values().update(spreadsheetId=sid, range="Outreach!A2", valueInputOption="RAW", body={"values": rows}))
