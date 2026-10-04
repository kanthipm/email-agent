"""Start the agent. If an earlier instance is still running (left over from sleep/resume or a
scheduled restart), stop it first so the port is free and no stuck thread survives."""
import os
import signal
import time

import uvicorn

from app import config

PID_FILE = config.ROOT / "logs" / "agent.pid"


def _replace_previous() -> None:
    PID_FILE.parent.mkdir(exist_ok=True)
    try:
        old = int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        old = 0
    if old and old != os.getpid():
        try:
            os.kill(old, signal.SIGTERM)
            time.sleep(2)
        except OSError:
            pass
    PID_FILE.write_text(str(os.getpid()))


if __name__ == "__main__":
    _replace_previous()
    uvicorn.run("app.server:app", host="0.0.0.0", port=config.PORT)
