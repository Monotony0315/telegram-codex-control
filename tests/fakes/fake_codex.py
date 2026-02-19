#!/usr/bin/env python3
"""Minimal fake codex process for tests."""

from __future__ import annotations

import signal
import sys
import time


running = True


def _stop(_sig: int, _frame: object) -> None:
    global running
    running = False


signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)

prompt = sys.argv[1] if len(sys.argv) > 1 else ""
print(f"fake-codex-start {prompt}", flush=True)

for i in range(40):
    if not running:
        print("fake-codex-interrupted", flush=True)
        sys.exit(130)
    print(f"tick-{i}", flush=True)
    time.sleep(0.05)

print("fake-codex-done", flush=True)
