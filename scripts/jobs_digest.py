"""Build (and optionally send) today's new-grad job digest now.

    python scripts/jobs_digest.py           # print the digest, send nothing
    python scripts/jobs_digest.py --send    # email it
    python scripts/jobs_digest.py --no-web  # skip the web-search pass
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import jobs, store  # noqa: E402


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    store.init()
    if "--send" in sys.argv:
        print(f"sent digest with {jobs.send()} postings")
        return
    subject, body, found = jobs.build(web="--no-web" not in sys.argv)
    print(subject, "\n" + "=" * len(subject) + "\n" + body)


if __name__ == "__main__":
    main()
