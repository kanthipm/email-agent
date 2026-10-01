"""Startup briefs for outreach targets: a web-researched summary of what the company does, its
stage and funding, hiring signals, risks and opportunities, and why the user fits, plus a
LinkedIn-length note to the founder."""
from __future__ import annotations

import json
import logging

from app import config, llm, resume
from app.contacts import _search

log = logging.getLogger(__name__)


def research(company: str, role_title: str) -> str:
    queries = [f"{company} startup what it does", f"{company} raised funding round series valuation",
               f"{company} crunchbase OR techcrunch OR pitchbook", f"{company} hiring engineers team size growth",
               f"{company} layoffs OR lawsuit OR controversy OR competitors"]
    lines = []
    for q in queries:
        for r in _search(q, 4):
            lines.append(f"- {r.get('title', '')} | {r.get('href', '')} | {r.get('body', '')[:260]}")
    return "\n".join(dict.fromkeys(lines))[:9000]


def write(company: str, role_title: str, job_url: str, contact: dict) -> dict:
    """{brief, linkedin_note, hiring} for a startup target."""
    notes = research(company, role_title)
    who = f"{contact.get('name')} ({contact.get('title')})" if contact.get("name") else "the founder"
    prompt = (
        f"You are briefing {config.MY_NAME or 'the user'} ({resume.profile()}) on the startup \"{company}\", which "
        f"has a new-grad posting '{role_title}' ({job_url}).\n\nWeb research notes:\n{notes}\n\n"
        "Write plain text, no markdown. Reply with only JSON:\n"
        '{"brief": "<~180 words with these labelled lines: What they do: / Stage & funding: / Team & traction: / '
        'Hiring: (what the posting and other signals say about how actively they are hiring) / Risks: / '
        'Opportunities: / Why you fit: >", '
        f'"linkedin_note": "<a LinkedIn connection note to {who}, under 290 characters, first person, specific to the '
        'company and role, asks for a quick chat or pointer to the hiring manager; no hashtags, no emoji>", '
        '"hiring": "actively|some|unclear"}\n'
        "Only state facts supported by the notes; say 'not found' for anything the notes do not cover rather than guessing."
    )
    try:
        data = json.loads(llm._chat([{"role": "system", "content": llm._persona()}, {"role": "user", "content": prompt}],
                                    json_mode=True, max_tokens=2500, temperature=0.3).get("content") or "{}")
    except (RuntimeError, json.JSONDecodeError) as e:
        log.warning("brief failed for %s: %s", company, e)
        return {"brief": "", "linkedin_note": "", "hiring": "unclear"}
    if isinstance(data, list):
        data = next((d for d in data if isinstance(d, dict)), {})
    if not isinstance(data, dict):
        data = {}
    return {"brief": str(data.get("brief", "")).strip(), "linkedin_note": str(data.get("linkedin_note", "")).strip()[:300],
            "hiring": str(data.get("hiring", "unclear"))}
