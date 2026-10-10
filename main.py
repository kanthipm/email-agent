"""Start the agent, or do nothing if a healthy instance is already running.

The scheduled task calls this at logon, twice a day, and every 30 minutes as a watchdog. A
previous instance that answers /health is left alone; one that is hung or dead is replaced."""
import os
import signal
import time

import httpx
import uvicorn

from app import config

PID_FILE = config.ROOT / "logs" / "agent.pid"


def _healthy() -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{config.PORT}/health", timeout=5).status_code == 200
    except Exception:
        return False


def _take_over() -> bool:
    PID_FILE.parent.mkdir(exist_ok=True)
    try:
        old = int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        old = 0
    if old and old != os.getpid():
        if _healthy():
            return False
        try:
            os.kill(old, signal.SIGTERM)
            time.sleep(3)
        except OSError:
            pass
    PID_FILE.write_text(str(os.getpid()))
    return True


if __name__ == "__main__":
    if _take_over():
        uvicorn.run("app.server:app", host="0.0.0.0", port=config.PORT)
