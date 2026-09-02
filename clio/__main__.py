"""Entrypoint: `python -m clio`. Starts the core service, runs until interrupted, exits clean."""

import signal
import sys
import time


def main() -> int:
    print("Clio starting...")

    running = True

    def _stop(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _stop)

    print("Clio is running. Press Ctrl+C to stop.")
    while running:
        time.sleep(0.2)

    print("Clio shutting down...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
