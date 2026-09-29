"""The user's resume: read from PDF/DOCX/text, summarized once into a short profile that the
job ranking and the email drafter use. Re-summarized whenever the file changes."""
from __future__ import annotations

import logging
from pathlib import Path

from app import config, store

log = logging.getLogger(__name__)
MAX_CHARS = 12000


def text() -> str:
    p = Path(config.RESUME_PATH)
    if not p.exists():
        return ""
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader
        raw = "\n".join(page.extract_text() or "" for page in PdfReader(str(p)).pages)
    elif suffix == ".docx":
        import docx
        raw = "\n".join(par.text for par in docx.Document(str(p)).paragraphs)
    else:
        raw = p.read_text(encoding="utf-8", errors="ignore")
    return " ".join(raw.split())[:MAX_CHARS]


def profile() -> str:
    """JOBS_PROFILE if set, else a cached model-written summary of the resume, else a generic line."""
    if config.JOBS_PROFILE:
        return config.JOBS_PROFILE
    p = Path(config.RESUME_PATH)
    if not p.exists():
        return "a new grad looking for product management and software engineering roles"
    stamp = f"{p.stat().st_mtime_ns}:{p.stat().st_size}"
    cached = store.get_kv("resume_profile")
    if cached and cached.get("stamp") == stamp:
        return cached["text"]
    from app import llm
    raw = text()
    prompt = (
        "Summarize this resume in at most 120 words as a job-search profile: degree and graduation date, "
        "location and any location preferences you can infer, strongest technical skills, notable experience "
        "(companies, projects, founder work), and what kinds of new-grad roles fit best (e.g. APM, SWE, "
        "ML). Plain prose, no headings, no bullet points.\n\n" + raw
    )
    summary = (llm._chat([{"role": "user", "content": prompt}], max_tokens=600, temperature=0.2)
               .get("content") or "").strip()
    if summary:
        store.set_kv("resume_profile", {"stamp": stamp, "text": summary})
        log.info("resume profile refreshed from %s", p.name)
    return summary or "a new grad looking for product management and software engineering roles"
