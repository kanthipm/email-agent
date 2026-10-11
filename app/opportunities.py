"""Daily scan for fellowships, grants, scholarships, accelerators and competitions with upcoming
deadlines, matched to the user personally and to their startup. Web search feeds snippets to the
model, which extracts only dated, sourced entries; results are remembered so each digest shows
what is new plus every deadline inside the next few weeks."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta

import httpx

from app import config, llm, resume, store
from app.contacts import _search
from app.mail.gmail import html_to_text

log = logging.getLogger(__name__)
UPCOMING_DAYS = 21
KINDS = ("fellowship", "grant", "scholarship", "accelerator", "competition", "program")

QUERIES = [
    # startup / founder programs (student-founder first)
    "student founder fellowship applications open deadline {year}",
    "university student startup accelerator applications deadline {year}",
    "health tech startup accelerator applications open {year} deadline",
    "digital health pre-seed grant program applications {year}",
    "pre-seed startup competition applications open {year} prize",
    "Y Combinator Pear Neo Z Fellows Thiel Fellowship application deadline {year}",
    "non-dilutive grant early stage healthcare AI startup {year} apply",
    # personal scholarships / fellowships
    "computer science undergraduate scholarship application deadline {year} {next}",
    "AI machine learning fellowship for undergraduates applications {year}",
    "Duke University fellowship undergraduate application deadline {year}",
    "graduating senior fellowship entrepreneurship {next} applications",
    "women in tech scholarship computer science {year} deadline",
    # entrepreneurship programs
    "entrepreneurship fellowship for recent graduates {next} applications open",
    "founder residency program applications open {year} students",
]


# Named programs worth checking by name; a rotating slice is searched each day so every program
# is refreshed about weekly without hammering the search engine.
PROGRAMS = [
    # student / young founder
    "Thiel Fellowship", "Z Fellows", "Neo Scholars", "Pear VC Competition", "Contrary Talent Fellowship",
    "Dorm Room Fund", "1517 Fund Medici Project grant", "Emergent Ventures grant", "Y Combinator application",
    "a16z speedrun", "Soma Capital Fellowship", "8VC Fellowship", "Interact Fellowship", "Pioneer program",
    "Kleiner Perkins Fellows", "Bessemer Fellows", "Afore Capital founder fellowship", "Techstars Anywhere",
    "VentureWell E-Team grant", "NSF I-Corps", "Hult Prize", "Rice Business Plan Competition",
    "MIT 100K competition", "Duke Startup Challenge", "Duke Innovation and Entrepreneurship grant",
    "Duke Melissa and Doug Entrepreneurs program", "Duke Incubation Fund",
    # health-tech
    "MassChallenge HealthTech", "Cedars-Sinai Accelerator", "Texas Medical Center Innovation accelerator",
    "Nex Cubed health accelerator", "Health Wildcatters", "AlphaLab Health", "Mayo Clinic Platform Accelerate",
    "UCSF Rosenman Institute accelerator", "Johnson and Johnson JLABS", "AWS Healthcare Accelerator",
    "Google for Startups Accelerator AI", "NVIDIA Inception", "Microsoft for Startups Founders Hub",
    # personal scholarships / fellowships
    "NCWIT Collegiate Award", "Grace Hopper Celebration scholarship", "Rewriting the Code fellowship",
    "Palantir Women in Technology Scholarship", "Generation Google Scholarship", "Adobe Research Women in Technology",
    "Quad Fellowship", "Schwarzman Scholars", "Knight-Hennessy Scholars", "Fulbright US student program",
    "Marshall Scholarship", "Churchill Scholarship", "Venture for America fellowship", "Kauffman Fellows",
]
ROTATE = 10


_DATE_HINT = re.compile(r"deadline|apply by|applications? (close|due|open)|due date|closes on|last day to apply", re.I)


def _page_excerpt(url: str) -> str:
    """Lines from the page that mention a deadline, so the extractor sees dates the snippet lacked."""
    try:
        r = httpx.get(url, timeout=10, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            return ""
    except Exception:
        return ""
    text = html_to_text(r.text)
    hits = [ln.strip() for ln in text.splitlines() if _DATE_HINT.search(ln) and len(ln.strip()) > 15]
    return " | ".join(dict.fromkeys(hits))[:700]


def _gather(queries: list[str], fetch_pages: bool) -> str:
    seen, lines = set(), []
    for q in queries:
        for i, r in enumerate(_search(q, 5)):
            key = r.get("href", "")
            if not key or key in seen:
                continue
            seen.add(key)
            extra = _page_excerpt(key) if fetch_pages and i == 0 else ""
            lines.append(f"- {r.get('title', '')} | {key} | {r.get('body', '')[:240]}" + (f" | PAGE: {extra}" if extra else ""))
    return "\n".join(lines)[:14000]


def _extract(today: date, notes: str) -> list[dict]:
    prompt = (
        f"Today is {today.isoformat()}. From the web search notes below, list fellowships, grants, scholarships, "
        f"accelerators, competitions and programs that {config.MY_NAME or 'the user'} or their startup could "
        f"realistically apply to.\nAbout them: {resume.profile()}\nTheir startup: {config.COMPANY_PROFILE}\n\n"
        "Rules: include an entry only if the notes give a specific application deadline or open window on or "
        "after today (write 'rolling' if the notes say applications are rolling/always open; if only a month is "
        "given, use the last day of that month). Skip anything "
        "already closed, anything with no date, and anything clearly for other countries, high-schoolers, "
        "PhDs or faculty. Prefer student and early-stage programs. Keep the URL exactly as given.\n"
        'Reply with only JSON: {"opportunities": [{"name": "", "org": "", "kind": "fellowship|grant|scholarship|'
        'accelerator|competition|program", "for": "medpull|personal|both", "deadline": "YYYY-MM-DD|rolling", '
        '"amount": "", "eligibility": "<one line>", "why": "<one line on fit>", "url": ""}]}\n\n' + notes
    )
    try:
        data = json.loads(llm._chat([{"role": "user", "content": prompt}], json_mode=True, max_tokens=4000,
                                    temperature=0.2).get("content") or "{}")
    except (RuntimeError, json.JSONDecodeError) as e:
        log.warning("opportunity extraction failed: %s", e)
        return []
    if isinstance(data, list):
        data = {"opportunities": data}
    out = []
    for o in (data.get("opportunities") or []) if isinstance(data, dict) else []:
        if not isinstance(o, dict) or not o.get("name") or not o.get("url"):
            continue
        dl = str(o.get("deadline", "")).strip()
        if dl != "rolling":
            try:
                if date.fromisoformat(dl) < today:
                    continue
            except ValueError:
                continue
        out.append({k: str(o.get(k, "")).strip() for k in ("name", "org", "kind", "for", "deadline", "amount",
                                                           "eligibility", "why", "url")})
    return out


def refresh(today: date, full: bool = False) -> tuple[list[dict], list[dict]]:
    """Search, extract, remember. Returns (newly found, everything due within UPCOMING_DAYS).
    Daily: the generic queries plus a rotating slice of named programs. The first run, or
    full=True, checks every named program."""
    year, nxt = today.year, today.year + 1
    if full or not store.get_kv("opps_full_run"):
        names = list(PROGRAMS)
    else:
        start = (today.toordinal() * ROTATE) % len(PROGRAMS)
        names = [PROGRAMS[(start + i) % len(PROGRAMS)] for i in range(ROTATE)]
    batches = [[q.format(year=year, next=nxt) for q in QUERIES]]
    batches += [[f"{n} {year} application deadline" for n in names[i:i + ROTATE]] for i in range(0, len(names), ROTATE)]
    found: list[dict] = []
    for i, batch in enumerate(batches):
        notes = _gather(batch, fetch_pages=i > 0)
        if notes:
            found += _extract(today, notes)
    if len(names) == len(PROGRAMS):
        store.set_kv("opps_full_run", today.isoformat())
    new = [o for o in found if store.upsert_opportunity(o)]
    store.set_kv("opps_last_run", today.isoformat())
    return new, upcoming(today)


def upcoming(today: date) -> list[dict]:
    horizon = (today + timedelta(days=UPCOMING_DAYS)).isoformat()
    rows = [r for r in store.opportunities() if r["deadline"] == "rolling" or today.isoformat() <= r["deadline"] <= horizon]
    rows.sort(key=lambda r: (r["deadline"] == "rolling", r["deadline"], r["for"] != "medpull"))
    return rows


def _fmt(o: dict) -> list[str]:
    tag = {"medpull": "[MedPull]", "personal": "[you]", "both": "[you + MedPull]"}.get(o["for"], "")
    due = "rolling" if o["deadline"] == "rolling" else o["deadline"]
    head = f"- {o['name']} ({o['org']}) {tag} — {o['kind']}, due {due}" + (f", {o['amount']}" if o["amount"] else "")
    return [head, f"  {o['url']}", f"  {o['eligibility']} {o['why']}".strip()]


def render(new: list[dict], due: list[dict]) -> str:
    if not new and not due:
        return ""
    lines = ["", "FELLOWSHIPS, GRANTS, SCHOLARSHIPS, PROGRAMS", ""]
    if due:
        lines.append(f"Deadlines in the next {UPCOMING_DAYS} days (student and MedPull-eligible first):")
        for o in due:
            lines += _fmt(o)
        lines.append("")
    if new:
        due_urls = {o["url"] for o in due}
        fresh = [o for o in new if o["url"] not in due_urls]
        if fresh:
            lines.append("Newly found (later deadlines):")
            for o in fresh:
                lines += _fmt(o)
    return "\n".join(lines)


def status_text(today: date | None = None) -> str:
    today = today or date.today()
    rows = upcoming(today)
    if not rows:
        return "No deadlines in the next three weeks."
    return "\n".join(f"{o['deadline']}: {o['name']} ({o['org']}) {o['for']} — {o['url']}" for o in rows)
