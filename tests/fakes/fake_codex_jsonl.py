#!/usr/bin/env python3
"""Fake Codex JSONL producer for Rust live-core contract tests."""

from __future__ import annotations

import json
import os
import sys


def _emit(payload: dict, *, stream: str) -> None:
    target = sys.stderr if stream == "stderr" else sys.stdout
    print(json.dumps(payload, ensure_ascii=True), file=target, flush=True)


def main() -> int:
    args = sys.argv[1:]
    mode = "exec"
    thread_id = "thread-exec"
    prompt = ""
    stream = os.environ.get("FAKE_CODEX_STREAM", "stdout").strip().lower()
    if stream not in {"stdout", "stderr"}:
        stream = "stdout"
    variant = os.environ.get("FAKE_CODEX_VARIANT", "default").strip().lower()

    if args and args[0] == "exec":
        if len(args) > 1 and args[1] == "resume":
            mode = "resume"
            thread_id = args[3] if len(args) > 3 else "thread-resume"
            if "--" in args:
                prompt = args[args.index("--") + 1]
        else:
            if "--" in args:
                prompt = args[args.index("--") + 1]
    _emit({"type": "thread.started", "thread_id": thread_id}, stream=stream)
    _emit({"type": "agent.updated", "status": "running", "message": f"{mode}:{prompt}"}, stream=stream)
    if variant == "item_completed":
        _emit(
            {
                "type": "item.completed",
                "item": {"id": "item_2", "type": "agent_message", "text": f"Hello from {mode}"},
            },
            stream=stream,
        )
    else:
        _emit({"type": "response.output_text.delta", "delta": "Hello"}, stream=stream)
        _emit({"type": "response.output_text.done", "text": f"Hello from {mode}"}, stream=stream)
    _emit({"type": "turn.completed"}, stream=stream)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
