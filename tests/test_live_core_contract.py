from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "rust" / "live-core" / "Cargo.toml"
FAKE_CODEX = PROJECT_ROOT / "tests" / "fakes" / "fake_codex_jsonl.py"


def _run_live_core(workspace_root: Path, *mode_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "cargo",
            "run",
            "--quiet",
            "--manifest-path",
            str(MANIFEST_PATH),
            "--",
            "--workspace-root",
            str(workspace_root),
            "--codex-bin",
            sys.executable,
            "--codex-arg",
            str(FAKE_CODEX),
            *mode_args,
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )


def _run_live_core_with_env(workspace_root: Path, env_overrides: dict[str, str], *mode_args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(env_overrides)
    return subprocess.run(
        [
            "cargo",
            "run",
            "--quiet",
            "--manifest-path",
            str(MANIFEST_PATH),
            "--",
            "--workspace-root",
            str(workspace_root),
            "--codex-bin",
            sys.executable,
            "--codex-arg",
            str(FAKE_CODEX),
            *mode_args,
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )


def test_live_core_exec_emits_normalized_ndjson(workspace_root: Path) -> None:
    result = _run_live_core(workspace_root, "exec", "--", "hello")
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

    assert lines == [
        {"event_type": "session", "thread_id": "thread-exec"},
        {"event_type": "status", "status": "running", "message": "exec:hello"},
        {"event_type": "text_delta", "message": "Hello"},
        {"event_type": "text_done", "message": "Hello from exec"},
        {"event_type": "done"},
    ]


def test_live_core_resume_emits_resume_session(workspace_root: Path) -> None:
    result = _run_live_core(workspace_root, "resume", "thread-prev", "--", "continue")
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

    assert lines[0] == {"event_type": "session", "thread_id": "thread-prev"}
    assert lines[1] == {"event_type": "status", "status": "running", "message": "resume:continue"}
    assert lines[-1] == {"event_type": "done"}


def test_live_core_normalizes_events_from_stderr(workspace_root: Path) -> None:
    result = _run_live_core_with_env(
        workspace_root,
        {"FAKE_CODEX_STREAM": "stderr"},
        "exec",
        "--",
        "stderr-case",
    )
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

    assert lines == [
        {"event_type": "session", "thread_id": "thread-exec"},
        {"event_type": "status", "status": "running", "message": "exec:stderr-case"},
        {"event_type": "text_delta", "message": "Hello"},
        {"event_type": "text_done", "message": "Hello from exec"},
        {"event_type": "done"},
    ]


def test_live_core_preserves_unknown_json_events_for_python_fallback_parsing(workspace_root: Path) -> None:
    result = _run_live_core_with_env(
        workspace_root,
        {"FAKE_CODEX_VARIANT": "item_completed"},
        "exec",
        "--",
        "item-case",
    )
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

    assert lines == [
        {"event_type": "session", "thread_id": "thread-exec"},
        {"event_type": "status", "status": "running", "message": "exec:item-case"},
        {
            "type": "item.completed",
            "item": {"id": "item_2", "type": "agent_message", "text": "Hello from exec"},
        },
        {"event_type": "done"},
    ]
