"""Feed parsing for the job digest, offline."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import jobs  # noqa: E402

README = """
| Company | Job Title | Location | Work Model | Date Posted |
| **[PlanetArt](http://www.planetart.com)** | **[Associate Product Manager, Web](https://jobright.ai/jobs/info/1)** | Calabasas, CA, United States | Hybrid | Sep 28 |
| ↳ | **[APM, Mobile](https://jobright.ai/jobs/info/2)** | Remote | Remote | Sep 27 |
| **[OldCo](http://old.com)** | **[PM](https://jobright.ai/jobs/info/3)** | NYC | On Site | Sep 01 |
| **[NewYear](http://ny.com)** | **[PM](https://jobright.ai/jobs/info/4)** | NYC | On Site | Dec 31 |
"""


def test_jobright_parse(monkeypatch):
    class R:
        status_code = 200
        text = README

        def raise_for_status(self):
            pass

    monkeypatch.setattr(jobs.httpx, "get", lambda *a, **k: R())
    today = date(2026, 9, 28)
    got = jobs._jobright("PM", "x/y", date(2026, 9, 27), today)
    assert [(j.company, j.title, j.posted) for j in got] == [
        ("PlanetArt", "Associate Product Manager, Web", "2026-09-28"),
        ("PlanetArt", "APM, Mobile", "2026-09-27"),           # continuation row inherits the company
    ]
    assert jobs._jobright_date("Dec 31", date(2026, 1, 2)) == date(2025, 12, 31)   # year rollover


def test_dedupe(monkeypatch):
    monkeypatch.setattr(jobs.config, "JOBS_STRICT_NEW_GRAD", False)
    monkeypatch.setattr(jobs, "_simplify", lambda since: [jobs.Job("PM", "Acme", "APM", "u1", "NYC", "2026-09-28", "simplify")])
    monkeypatch.setattr(jobs, "_jobright", lambda *a: [jobs.Job("PM", "ACME", "APM!", "u2", "NYC", "2026-09-28", "jobright"),
                                                       jobs.Job("SWE", "Beta", "SWE I", "u3", "SF", "2026-09-28", "jobright")])
    got = jobs.collect(date(2026, 9, 27), date(2026, 9, 28), web=False)
    assert [j.url for j in got] == ["u1", "u3"]


def test_render_orders_and_drops():
    J = jobs.Job
    scored = [(J("PM", "Acme", "APM", "u1", "NYC", "2026-09-28", "x"), 5, "great fit"),
              (J("SWE", "Beta", "SWE I", "u2", "SF", "2026-09-28", "x"), 3, ""),
              (J("SWE", "Old", "Staff Eng", "u3", "SF", "2026-09-28", "x"), 0, "senior")]
    body = jobs.render(scored, date(2026, 9, 27))
    assert body.index("TOP PRIORITY") < body.index("u1") < body.index("ALSO NEW TODAY: SOFTWARE") < body.index("u2")
    assert "u3" not in body and "1 non-new-grad postings dropped" in body
    assert "ALSO NEW TODAY: PRODUCT" not in body   # the only PM posting is already in top


def test_strict_new_grad_gate():
    J = lambda title, src="jobright": jobs.Job("SWE", "X", title, "u", "", "2026-09-28", src)
    assert jobs.is_new_grad(J("Software Engineer, New Grad"))
    assert jobs.is_new_grad(J("Entry Level Technical Product Manager - Austin, TX - 2027"))
    assert jobs.is_new_grad(J("Software Engineer I / II"))
    assert jobs.is_new_grad(J("Associate Product Manager"))
    assert jobs.is_new_grad(J("Software Engineer", "simplify"))          # curated feed trusted
    assert not jobs.is_new_grad(J("Software Engineer"))                  # scraped, no signal
    assert jobs.is_new_grad(J("Senior Software Engineer, New Grad Team"))   # explicit phrase wins
    assert not jobs.is_new_grad(J("Software Engineer II"))
    assert not jobs.is_new_grad(J("Product Strategy Analyst III"))
    assert not jobs.is_new_grad(J("Staff Engineer", "simplify"))         # seniority beats trust
