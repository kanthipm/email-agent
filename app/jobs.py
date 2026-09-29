"""Daily new-grad job digest: collect today's SWE and PM postings from public feeds (plus an
optional capped web search), have the model rank them, and email the list to the user."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

import httpx

from app import config, llm, resume, store
from app.mail import providers

log = logging.getLogger(__name__)

SIMPLIFY_URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"
JOBRIGHT_REPOS = {
    "PM": "jobright-ai/2026-Product-Management-New-Grad",
    "SWE": "jobright-ai/2026-Software-Engineer-New-Grad",
}
SIMPLIFY_CATEGORIES = {"Software": "SWE", "Software Engineering": "SWE", "Product": "PM"}
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


@dataclass
class Job:
    track: str            # "SWE" | "PM"
    company: str
    title: str
    url: str
    location: str
    posted: str           # YYYY-MM-DD
    source: str


# ---------------------------------------------------------------- sources
def _simplify(since: date) -> list[Job]:
    r = httpx.get(SIMPLIFY_URL, timeout=60, follow_redirects=True)
    r.raise_for_status()
    out = []
    for x in r.json():
        track = SIMPLIFY_CATEGORIES.get(x.get("category"))
        if not track or not x.get("active") or not x.get("is_visible", True):
            continue
        posted = date.fromtimestamp(x.get("date_posted", 0))
        if posted < since:
            continue
        out.append(Job(track, x.get("company_name", ""), x.get("title", ""), x.get("url", ""),
                       ", ".join(x.get("locations") or []), posted.isoformat(), "simplify"))
    return out


def _jobright_date(text: str, today: date) -> date | None:
    m = re.match(r"([A-Z][a-z]{2}) (\d{1,2})", text.strip())
    if not m or m.group(1) not in _MONTHS:
        return None
    d = date(today.year, _MONTHS[m.group(1)], int(m.group(2)))
    return d.replace(year=today.year - 1) if d > today + timedelta(days=2) else d


def _jobright(track: str, repo: str, since: date, today: date) -> list[Job]:
    r = httpx.get(f"https://raw.githubusercontent.com/{repo}/master/README.md", timeout=60, follow_redirects=True)
    r.raise_for_status()
    out, company = [], ""
    for line in r.text.splitlines():
        if not line.startswith("|") or "http" not in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        posted = _jobright_date(cells[4], today)
        if posted is None or posted < since:
            continue
        c = _MD_LINK.search(cells[0])
        if c:
            company = c.group(1).strip("* ")
        t = _MD_LINK.search(cells[1])
        if not t:
            continue
        out.append(Job(track, company, t.group(1).strip("* "), t.group(2), cells[2], posted.isoformat(), "jobright"))
    return out


def _web_search(today: date) -> list[Job]:
    """Capped supplementary pass on the small model (separate quota). Failures are ignored."""
    prompt = (f"Today is {today.isoformat()}. Use web search (at most 4 searches) to find new grad / early career "
              "Software Engineer and Product Manager / APM job postings in the US published in the last 24 hours. "
              "Reply with ONLY a JSON array of objects with keys company, title, url, location, track "
              '("SWE" or "PM"). Only include postings whose URL you actually saw. Up to 8 items.')
    if not config.GROQ_API_KEY:
        return []          # browser_search is a Groq-only tool
    body = {"model": config.JOBS_SEARCH_MODEL, "messages": [{"role": "user", "content": prompt}],
            "tools": [{"type": "browser_search"}], "max_tokens": 4000}
    r = httpx.post("https://api.groq.com/openai/v1/chat/completions", json=body, timeout=180,
                   headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"})
    if r.status_code >= 400:
        log.warning("web search skipped: Groq %s", r.status_code)
        return []
    text = r.json()["choices"][0]["message"].get("content") or ""
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return [Job(str(i.get("track", "SWE")).upper(), i.get("company", ""), i.get("title", ""), i.get("url", ""),
                i.get("location", ""), today.isoformat(), "web") for i in items if i.get("url")]


def collect(since: date, today: date, web: bool = True) -> list[Job]:
    jobs: list[Job] = []
    for name, fn in [("simplify", lambda: _simplify(since)),
                     ("jobright PM", lambda: _jobright("PM", JOBRIGHT_REPOS["PM"], since, today)),
                     ("jobright SWE", lambda: _jobright("SWE", JOBRIGHT_REPOS["SWE"], since, today))]:
        try:
            jobs += fn()
        except Exception as e:
            log.warning("job source %s failed: %s", name, e)
    if web and config.JOBS_WEB_SEARCH:
        try:
            jobs += _web_search(today)
        except Exception as e:
            log.warning("web search failed: %s", e)
    seen, out, dropped = set(), [], 0
    for j in jobs:
        key = (j.company.lower(), re.sub(r"\W+", " ", j.title.lower()).strip())
        if key in seen or not j.url:
            continue
        seen.add(key)
        if config.JOBS_STRICT_NEW_GRAD and not is_new_grad(j):
            dropped += 1
            continue
        out.append(j)
    if dropped:
        log.info("strict new-grad filter dropped %d postings", dropped)
    return out


# ---------------------------------------------------------------- strict new-grad filter
_NEW_GRAD = re.compile(
    r"new ?grad|graduate|entry[- ]level|early[- ]career|early[- ]talent|associate product manager|\bapm\b|"
    r"university|campus|rotational|\b20(26|27)\b|junior|\bjr\.?\b|\b(engineer|developer|analyst|scientist)\s+(i|1)\b|"
    r"\banalyst\b|\bprogram\b|fellow|trainee|\bco-?op\b|\bintern", re.I)
_SENIOR = re.compile(
    r"\bsenior\b|\bsr\.?\b|\bstaff\b|principal|\blead\b|director|\bhead of\b|\bvp\b|vice president|architect|"
    r"experienced|mid[- ]level|\b(iii|iv)\b|\b[3-9]\+?\s*years?\b", re.I)
_EXPLICIT = re.compile(r"new ?grad|entry[- ]level|graduate|early[- ]career", re.I)


def is_new_grad(job: Job) -> bool:
    """Title-level gate: seniority words disqualify unless the title says new-grad outright;
    scraped sources must carry a new-grad signal, the curated Simplify feed is trusted as-is."""
    t = job.title
    senior = bool(_SENIOR.search(t)) or (bool(re.search(r"\bII\b", t)) and not re.search(r"\bI\b", t))
    if senior and not _EXPLICIT.search(t):
        return False
    return bool(_NEW_GRAD.search(t)) or job.source == "simplify"



# ---------------------------------------------------------------- ranking + email
BATCH = 20


def _score_batch(batch: list[Job], today: date) -> dict[int, tuple[int, str]]:
    """Ask the model for a 0-5 priority per posting. 0 means not a new-grad role at all."""
    profile = resume.profile()
    listing = "\n".join(f"{i}. [{j.track}] {j.company} — {j.title} — {j.location}" for i, j in enumerate(batch, 1))
    prompt = (
        f"Rate new-grad job postings for {config.MY_NAME or 'the user'}, {profile}. Today is {today.isoformat()}.\n"
        "Every posting MUST be a genuine new-grad / entry-level role (0-2 years, 2026-2027 grads). "
        "Score each 0-5: 5 = apply today (strong company, unmistakably new-grad/APM/early-career, good fit); "
        "3 = worth a look; 1 = weak fit; 0 = not a new-grad role (senior, staff, manager of managers, 5+ years) "
        "or not a software/tech product role (retail merchandising, fashion or consumer-goods 'product' jobs, "
        "non-technical coordinator roles).\n"
        'Reply with only JSON: {"scores": [{"i": <number>, "score": <0-5>, "why": "<max 12 words>"}]} '
        "covering every number.\n\n" + listing
    )
    try:
        msg = llm._chat([{"role": "user", "content": prompt}], json_mode=True, max_tokens=3000, temperature=0.2)
        data = json.loads(msg.get("content") or "{}")
    except RuntimeError as e:
        if "json_validate_failed" not in str(e):
            raise
        # Reasoning models sometimes return nothing in JSON mode; ask again free-form and parse leniently.
        msg = llm._chat([{"role": "user", "content": prompt}], max_tokens=3000, temperature=0.2)
        m = re.search(r"\{.*\}", msg.get("content") or "", re.S)
        data = json.loads(m.group(0)) if m else {}
    out: dict[int, tuple[int, str]] = {}
    for row in (data.get("scores") or []):
        try:
            i, score = int(row["i"]), max(0, min(5, int(row["score"])))
        except (KeyError, TypeError, ValueError):
            continue
        if 1 <= i <= len(batch):
            out[i] = (score, str(row.get("why", ""))[:120])
    return out


def rank(jobs: list[Job], today: date) -> tuple[list[tuple[Job, int, str]], bool]:
    """Returns (scored jobs, whether any batch was actually scored by the model)."""
    scored: list[tuple[Job, int, str]] = []
    any_scored = False
    for start in range(0, len(jobs), BATCH):
        batch = jobs[start:start + BATCH]
        try:
            scores = _score_batch(batch, today)
            any_scored = any_scored or bool(scores)
        except Exception as e:
            log.warning("scoring batch failed, keeping unscored: %s", e)
            scores = {}
        for i, j in enumerate(batch, 1):
            score, why = scores.get(i, (2, ""))
            scored.append((j, score, why))
    return scored, any_scored


def render(scored: list[tuple[Job, int, str]], since: date, ranked: bool = True) -> str:
    if not ranked:
        keep = scored
        lines = ["Ranking unavailable today (model quota exhausted); full unranked list below.", ""]
        for track, label in (("PM", "PRODUCT"), ("SWE", "SOFTWARE")):
            lines += ["", label, ""]
            lines += [f"- {j.company} — {j.title} — {j.location}\n  {j.url}" for j, _, _ in keep if j.track == track]
        lines += ["", f"Sources: SimplifyJobs, Jobright. Postings since {since.isoformat()}."]
        return "\n".join(lines)
    keep = [t for t in scored if t[1] > 0]
    top = sorted([t for t in keep if t[1] >= 4], key=lambda t: -t[1])[:10]
    top_urls = {t[0].url for t in top}
    lines = ["TOP PRIORITY", ""]
    if not top:
        lines.append("Nothing stood out today; see the full list below.")
    for n, (j, _, why) in enumerate(top, 1):
        lines += [f"{n}. {j.company} — {j.title} — {j.location}", f"   {j.url}"] + ([f"   {why}"] if why else []) + [""]
    for track, label in (("PM", "ALSO NEW TODAY: PRODUCT"), ("SWE", "ALSO NEW TODAY: SOFTWARE")):
        rest = sorted([t for t in keep if t[0].track == track and t[0].url not in top_urls], key=lambda t: -t[1])
        if not rest:
            continue
        lines += ["", label, ""]
        for j, _, _ in rest:
            lines += [f"- {j.company} — {j.title} — {j.location}", f"  {j.url}"]
    lines += ["", f"Sources: SimplifyJobs, Jobright{', web search' if config.JOBS_WEB_SEARCH else ''}. "
              f"Postings since {since.isoformat()}; {len(scored) - len(keep)} non-new-grad postings dropped."]
    return "\n".join(lines)


def build(today: date | None = None, web: bool = True) -> tuple[str, str, list[Job]]:
    today = today or date.today()
    last = store.get_kv("jobs_digest_last_run")
    since = max(date.fromisoformat(last) if last else today - timedelta(days=1), today - timedelta(days=3))
    jobs = collect(since, today, web=web)
    subject = f"New grad jobs {today.strftime('%a %b %d')}: {len(jobs)} new SWE/PM postings"
    if not jobs:
        return subject, "Nothing new posted since the last digest.", []
    scored, ranked = rank(jobs, today)
    return subject, render(scored, since, ranked), jobs


def send(today: date | None = None) -> int:
    today = today or date.today()
    gmail = providers().get("gmail")
    if gmail is None:
        raise RuntimeError("Gmail is not configured; the digest is sent from Gmail")
    to = config.JOBS_DIGEST_TO or gmail.address
    subject, body, jobs = build(today)
    gmail.send([to], subject, body)
    store.set_kv("jobs_digest_last_run", today.isoformat())
    store.set_kv("jobs_digest_last", {"date": today.isoformat(), "count": len(jobs),
                                      "jobs": [asdict(j) for j in jobs]})
    log.info("job digest sent to %s: %d postings", to, len(jobs))
    return len(jobs)


def run_forever(stop: threading.Event) -> None:
    """Send once a day at JOBS_DIGEST_HOUR local time."""
    while not stop.is_set():
        now = datetime.now()
        if config.JOBS_DIGEST_ENABLED and now.hour >= config.JOBS_DIGEST_HOUR \
                and store.get_kv("jobs_digest_last_run") != now.date().isoformat():
            try:
                send(now.date())
            except Exception:
                log.exception("job digest failed")
                stop.wait(1800)
                continue
        stop.wait(60)
