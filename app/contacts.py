"""Free contact discovery for outreach: web search for the founder / hiring lead, their LinkedIn
profile, the company domain, any public address on the company site, and a likely email
pattern. Only an address found verbatim (or returned by Hunter.io) counts as verified; a
pattern guess is surfaced to the user but never used as a recipient on its own."""
from __future__ import annotations

import json
import logging
import re

import httpx

from app import config, llm

log = logging.getLogger(__name__)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_GENERIC = ("info", "hello", "hi", "contact", "support", "press", "sales", "team", "careers", "jobs",
            "recruiting", "talent", "hiring", "people", "founders", "admin", "noreply", "no-reply")


def _search(query: str, n: int) -> list[dict]:
    try:
        from ddgs import DDGS
        with DDGS() as d:
            return list(d.text(query, max_results=n))
    except Exception as e:
        log.warning("web search failed for %r: %s", query, e)
        return []


def _pick_person(company: str, kind: str, results: list[dict]) -> dict:
    """Model reads the search snippets and names the best person to contact."""
    role = ("a founder, co-founder, CEO or CTO" if kind == "startup"
            else "a university / early-career recruiter, or the engineering or product leader hiring new grads")
    snippets = "\n".join(f"- {r.get('title', '')} | {r.get('href', '')} | {r.get('body', '')[:200]}" for r in results)
    prompt = (
        f"From these web search results about the company \"{company}\", identify {role} at that exact company "
        "(ignore people at similarly named companies). Also identify the company's own website domain.\n"
        'Reply with only JSON: {"name": "", "title": "", "linkedin": "<linkedin.com/in/... url or empty>", '
        '"domain": "<example.com or empty>", "confidence": "high|medium|low"}. Use empty strings if unsure.\n\n'
        + snippets
    )
    try:
        data = json.loads(llm._chat([{"role": "user", "content": prompt}], json_mode=True, max_tokens=800,
                                    temperature=0.1).get("content") or "{}")
    except (RuntimeError, json.JSONDecodeError) as e:
        log.warning("contact pick failed for %s: %s", company, e)
        return {}
    return {k: str(data.get(k, "")).strip() for k in ("name", "title", "linkedin", "domain", "confidence")}


def _public_emails(domain: str) -> list[str]:
    """Addresses on the company's own domain that appear on its site or in search results."""
    found: set[str] = set()
    for path in ("", "/about", "/careers", "/contact", "/team", "/jobs"):
        try:
            r = httpx.get(f"https://{domain}{path}", timeout=10, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                found.update(m.group(0).lower() for m in _EMAIL.finditer(r.text) if m.group(1).lower() == domain)
        except Exception:
            pass
    for r in _search(f'"@{domain}" email', 6):
        text = f"{r.get('title', '')} {r.get('body', '')}"
        found.update(m.group(0).lower() for m in _EMAIL.finditer(text) if m.group(1).lower() == domain)
    return sorted(e for e in found if not any(e.endswith(x) for x in (".png", ".jpg", ".svg")))


def _guess(name: str, domain: str, samples: list[str]) -> str:
    """first.last / first / flast etc., inferred from a personal address seen on the domain, else first@."""
    parts = [p for p in re.sub(r"[^a-z ]", "", name.lower()).split() if p]
    if len(parts) < 2 or not domain:
        return ""
    first, last = parts[0], parts[-1]
    personal = [s.split("@")[0] for s in samples if s.split("@")[0] not in _GENERIC and "." not in s.split("@")[0][:1]]
    pattern = "first"
    for local in personal:
        if "." in local:
            pattern = "first.last"
            break
        if len(local) > 6 and local[0].isalpha() and not local.isdigit():
            pattern = "flast" if len(local) <= 8 else "firstlast"
    return {"first": f"{first}@{domain}", "first.last": f"{first}.{last}@{domain}",
            "flast": f"{first[0]}{last}@{domain}", "firstlast": f"{first}{last}@{domain}"}[pattern]


def find(company: str, kind: str = "startup") -> dict:
    """Best available contact for cold outreach at `company`.

    Returns {name, title, linkedin, domain, email, email_guess, public_emails, confidence}.
    `email` is set only when verified (seen verbatim on the domain, or from Hunter.io)."""
    q_person = (f"{company} founder CEO site:linkedin.com/in" if kind == "startup"
                else f"{company} university recruiter early career site:linkedin.com/in")
    results = _search(q_person, 6) + _search(f"{company} startup founders official site", 5)
    if not results:
        return {}
    person = _pick_person(company, kind, results)
    domain = re.sub(r"^https?://|^www\.|/.*$", "", person.get("domain", "")).lower()
    out = {"name": person.get("name", ""), "title": person.get("title", ""), "linkedin": person.get("linkedin", ""),
           "domain": domain, "email": "", "email_guess": "", "public_emails": [],
           "confidence": person.get("confidence", "low")}
    if not domain:
        return out
    public = _public_emails(domain)
    out["public_emails"] = public
    # A personal address for the named person found verbatim counts as verified.
    first = out["name"].split()[0].lower() if out["name"] else ""
    for e in public:
        local = e.split("@")[0]
        if first and local.startswith(first) and local not in _GENERIC:
            out["email"] = e
            break
    if not out["email"] and config.HUNTER_API_KEY:
        out["email"] = _hunter(domain, out["name"]) or ""
    if not out["email"]:
        out["email_guess"] = _guess(out["name"], domain, public)
    return out


def _hunter(domain: str, name: str) -> str:
    try:
        parts = name.split()
        if len(parts) >= 2:
            r = httpx.get("https://api.hunter.io/v2/email-finder",
                          params={"domain": domain, "first_name": parts[0], "last_name": parts[-1],
                                  "api_key": config.HUNTER_API_KEY}, timeout=30)
            if r.status_code == 200:
                return r.json().get("data", {}).get("email") or ""
    except Exception as e:
        log.warning("hunter lookup failed: %s", e)
    return ""
