"""Talk to the agent from the terminal instead of over SMS. Sends real email if you say "send".

    python scripts/chat.py            # interactive
    python scripts/chat.py --poll     # run one inbox scan first (texts you real drafts)
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import llm, poller, store  # noqa: E402


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    store.init()
    if "--poll" in sys.argv:
        print(f"drafts texted: {poller.poll_once()}")
    print("Type like you would text. Ctrl-C to quit.")
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text:
            print("\nagent>", llm.handle_sms(text))


if __name__ == "__main__":
    main()
