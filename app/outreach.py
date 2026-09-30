"""Cold outreach pipeline: each morning pick the best-fit companies from the ranked postings
(AI / health-tech startups first, then other startups, then big companies), find a founder or
recruiter address when possible, draft a short note in the user's voice, and follow up on sent
notes until a reply arrives. Every email still goes through the SMS approval gate."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

import httpx

from app import config, llm, resume, sms, store
from app.jobs import Job
from app.mail import providers

log = logging.getLogger(__name__)

_STARTUP_TITLES = ["founder", "co-founder", "ceo", "cto", "head of engineering", "vp engineering",
                   "vp of engineering", "head of product", "chief", "engineering manager", "recruit", "talent"]
_BIG_TITLES = ["university recruit", "campus", "early career", "recruit", "talent", "engineering manager",
               "hiring manager", "product manager"]


# ---------------------------------------------------------------- targets
def pick_targets(scored: list[tuple[Job, int, str]], n: int) -> list[tuple[Job, str]]:
    """Best-fit postings not already in the pipeline, one per company, startups first."""
    done = store.outreach_companies()
    cands = [(j, s, w) for j, s, w in scored if s >= 4 and j.company.lower() not in done]
    cands.sort(key=lambda t: (-(2 if t[0].kind == "startup" and t[0].ai_health else 1 if t[0].kind == "startup" else 0),
                              -t[1]))
    out, used = [], set()
    for j, _, why in cands:
        if j.company.lower() in used:
            continue
        used.add(j.company.lower())
        out.append((j, why))
        if len(out) >= n:
            break
    return out


# ---------------------------------------------------------------- contact lookup
def _guess_domain(company: str) -> str:
    msg = llm._chat([{"role": "user", "content":
                      f'What is the primary website domain of the company "{company}"? Reply with only JSON '
                      '{"domain": "example.com"} or {"domain": ""} if unsure.'}], json_mode=True, max_tokens=60)
    try:
        d = json.loads(msg.get("content") or "{}").get("domain", "")
    except json.JSONDecodeError:
        d = ""
    return re.sub(r"^https?://|^www\.|/.*$", "", str(d)).strip().lower()


def find_contact(job: Job) -> dict:
    """{name, title, email} via Hunter.io domain search when a key is set; empty dict otherwise."""
    if not config.HUNTER_API_KEY:
        return {}
    domain = _guess_domain(job.company)
    if not domain:
        return {}
    r = httpx.get("https://api.hunter.io/v2/domain-search",
                  params={"domain": domain, "api_key": config.HUNTER_API_KEY, "limit": 25}, timeout=30)
    if r.status_code != 200:
        log.warning("hunter %s for %s: %s", r.status_code, domain, r.text[:120])
        return {}
    people = r.json().get("data", {}).get("emails", [])
    wanted = _STARTUP_TITLES if job.kind == "startup" else _BIG_TITLES
    best, best_rank = None, len(wanted)
    for p in people:
        title = (p.get("position") or "").lower()
        for i, w in enumerate(wanted):
            if w in title and i < best_rank:
                best, best_rank = p, i
    if not best:
        return {}
    name = " ".join(x for x in (best.get("first_name"), best.get("last_name")) if x)
    return {"name": name, "title": best.get("position") or "", "email": best.get("value") or ""}


# ---------------------------------------------------------------- drafting
def _write(job: Job, why: str, contact: dict, followup: int = 0) -> tuple[str, str]:
    who = (f"{contact['name']}, {contact['title']} at {job.company}" if contact.get("name")
           else f"the founder or hiring lead at {job.company}")
    angle = "an AI / health-tech company, which is exactly the space they care most about" if job.ai_health \
        else ("a startup" if job.kind == "startup" else "a larger company")
    if followup:
        ask = (f"Write follow-up #{followup} to a cold email {config.MY_NAME or 'the user'} sent to {who} that got "
               "no reply. Two or three sentences, warm and light, restate the ask in one line, no guilt-tripping.")
    else:
        ask = (f"Write a cold email from {config.MY_NAME or 'the user'} to {who}. Under 130 words. Open with why "
               f"this company specifically ({job.company} is {angle}; the posting is '{job.title}', {why}). "
               "One concrete credential from the profile that is relevant. Say they have applied / are applying "
               "to that role and ask for a 15-minute call or an intro to whoever is hiring for it. "
               "No flattery padding, no buzzwords, no bullet points, plain text, first name sign-off.")
    prompt = (f"Profile: {resume.profile()}\n\n{ask}\n\nWrite it in the user's voice based on the style samples "
              "in the system prompt. Reply with only JSON {\"subject\": \"...\", \"body\": \"...\"}.")
    msg = llm._chat([{"role": "system", "content": llm._persona()}, {"role": "user", "content": prompt}],
                    json_mode=True, max_tokens=1200, temperature=0.5)
    data = json.loads(msg.get("content") or "{}")
    return str(data.get("subject", f"New grad {job.title} at {job.company}")).strip(), str(data.get("body", "")).strip()


def draft_for(job: Job, why: str) -> dict | None:
    gmail = providers().get("gmail")
    if gmail is None:
        return None
    contact = find_contact(job)
    subject, body = _write(job, why, contact)
    if not body:
        return None
    d = store.create_draft(account="gmail", to_addrs=[contact["email"]] if contact.get("email") else [],
                           subject=subject, body=body, source="outreach",
                           summary=f"cold outreach to {job.company} ({job.title})")
    return store.add_outreach(company=job.company, role_title=job.title, job_url=job.url, kind=job.kind,
                              contact_name=contact.get("name", ""), contact_title=contact.get("title", ""),
                              contact_email=contact.get("email", ""), draft_id=d["id"])


def run_daily(scored: list[tuple[Job, int, str]], today: date) -> list[dict]:
    if store.get_kv("outreach_last_run") == today.isoformat():
        return []
    rows = []
    for job, why in pick_targets(scored, config.OUTREACH_PER_DAY):
        try:
            row = draft_for(job, why)
            if row:
                rows.append(row)
        except Exception:
            log.exception("outreach draft for %s failed", job.company)
    store.set_kv("outreach_last_run", today.isoformat())
    if rows:
        lines = [f"{len(rows)} outreach drafts ready:"]
        for r in rows:
            to = r["contact_email"] or "no address yet"
            lines.append(f"#{r['draft_id']} {r['company']} ({r['role_title'][:40]}) -> {to}")
        lines.append("Text 'show #id' to read one, 'send #id' to send, or tell me the changes / the address.")
        sms.send_text("\n".join(lines))
        store.append_chat({"role": "user", "content": "[event] Morning outreach drafts were created and listed to the user by text."})
        store.append_chat({"role": "assistant", "content": "\n".join(lines)})
    return rows


# ---------------------------------------------------------------- after sending / follow-ups
def on_sent(draft_id: int, sent_message_id: str) -> None:
    row = store.outreach_by_draft(draft_id)
    if not row:
        return
    gmail = providers().get("gmail")
    thread_id = row["thread_id"]
    if gmail and not thread_id:
        try:
            thread_id = gmail.get_message(sent_message_id).thread_id
        except Exception as e:
            log.warning("could not read sent outreach thread: %s", e)
    days = config.OUTREACH_FOLLOWUP_DAYS
    now = datetime.now(timezone.utc)
    if row["stage"] == "drafted":
        nxt = (now + timedelta(days=days[0])).isoformat(timespec="seconds") if days else None
        store.update_outreach(row["id"], stage="sent", sent_at=now.isoformat(timespec="seconds"),
                              thread_id=thread_id, next_followup_at=nxt)
    else:
        k = row["followups_sent"] + 1
        gap = days[k] - days[k - 1] if k < len(days) else None
        store.update_outreach(row["id"], followups_sent=k, thread_id=thread_id,
                              next_followup_at=(now + timedelta(days=gap)).isoformat(timespec="seconds") if gap else None)


def _replied(gmail, row: dict) -> bool:
    if not row["thread_id"]:
        return False
    for m in gmail.get_thread(row["thread_id"]):
        if not m.is_from_me and m.date > (row["sent_at"] or ""):
            return True
    return False


def check_followups() -> list[str]:
    """Create follow-up drafts for sent notes with no reply; close out threads that got one."""
    gmail = providers().get("gmail")
    if gmail is None:
        return []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    notes = []
    for row in store.outreach_rows("sent"):
        try:
            if _replied(gmail, row):
                store.update_outreach(row["id"], stage="replied", next_followup_at=None)
                notes.append(f"{row['company']} replied to your outreach. Check the thread '{row['role_title'][:40]}'.")
                continue
            if not row["next_followup_at"] or row["next_followup_at"] > now:
                continue
            if row["followups_sent"] >= len(config.OUTREACH_FOLLOWUP_DAYS):
                store.update_outreach(row["id"], stage="closed", next_followup_at=None)
                continue
            prev = store.get_draft(row["draft_id"])
            original = gmail.get_message(prev["sent_message_id"]) if prev and prev.get("sent_message_id") else None
            job = Job("", row["company"], row["role_title"], row["job_url"], "", "", "outreach", row["kind"])
            subject, body = _write(job, "", {"name": row["contact_name"], "title": row["contact_title"]},
                                   followup=row["followups_sent"] + 1)
            d = store.create_draft(account="gmail", to_addrs=[row["contact_email"]] if row["contact_email"] else [],
                                   subject=subject, body=body, source="outreach",
                                   reply_to_message_id=original.id if original else None,
                                   thread_id=row["thread_id"], summary=f"follow-up to {row['company']}")
            store.update_outreach(row["id"], draft_id=d["id"])
            notes.append(f"Follow-up #{row['followups_sent'] + 1} to {row['company']} is draft #{d['id']}. "
                         "Text 'show #id' or 'send #id'.")
        except Exception:
            log.exception("follow-up check failed for %s", row["company"])
    if notes:
        sms.send_text("\n".join(notes))
        store.append_chat({"role": "user", "content": "[event] Outreach follow-up check ran; the user was texted."})
        store.append_chat({"role": "assistant", "content": "\n".join(notes)})
    return notes


def status_text() -> str:
    rows = store.outreach_rows()
    if not rows:
        return "No outreach yet."
    return "\n".join(f"#{r['id']} {r['company']} — {r['role_title'][:40]} — {r['stage']}"
                     f"{' (draft #' + str(r['draft_id']) + ')' if r['stage'] == 'drafted' else ''}"
                     f"{', follow-ups sent: ' + str(r['followups_sent']) if r['stage'] == 'sent' else ''}"
                     f"{' to ' + r['contact_email'] if r['contact_email'] else ' (no address)'}" for r in rows)
