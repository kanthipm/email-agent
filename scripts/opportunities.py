"""Scan for fellowship / grant / scholarship / program deadlines now.

    python scripts/opportunities.py          # daily-sized scan: print what is due and what is new
    python scripts/opportunities.py --full   # check every named program
    python scripts/opportunities.py --share  # send the weekly startup list now
"""
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import opportunities, store  # noqa: E402

logging.basicConfig(level=logging.WARNING)
store.init()
if "--share" in sys.argv:
    print("sent weekly list with", opportunities.share_send(date.today()), "programs")
    sys.exit()
new, due = opportunities.refresh(date.today(), full="--full" in sys.argv)
print(f"new: {len(new)}  due within {opportunities.UPCOMING_DAYS} days: {len(due)}  known: {len(store.opportunities())}")
print(opportunities.render(new, due) or "nothing dated found")
