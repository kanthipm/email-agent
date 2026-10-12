"""Push the opportunities and outreach trackers to the Google Sheet now, and pull Status / Notes back.

    python scripts/sheet_sync.py
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import sheets, store  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
store.init()
print("synced:", sheets.sync())
