from __future__ import annotations

import os
import stat

import pytest

from telegram_codex_control.store import ActiveJobExistsError, MAX_EVENT_ROWS, Store


def test_store_enforces_one_active_job(store: Store) -> None:
    first = store.create_job(command="run", prompt="one", status="RUNNING")
    assert first.status == "RUNNING"

    with pytest.raises(ActiveJobExistsError):
        store.create_job(command="run", prompt="two", status="RUNNING")

    store.set_job_status(first.id, "SUCCEEDED", exit_code=0)
    second = store.create_job(command="run", prompt="three", status="RUNNING")
    assert second.id > first.id


def test_store_claim_update_idempotent(store: Store) -> None:
    assert store.claim_update(10) is True
    assert store.claim_update(10) is False
    assert store.claim_update(9) is False
    assert store.get_last_update_id() == 10


def test_store_claim_update_with_event_is_idempotent(store: Store) -> None:
    initial_count = len(store.list_events(limit=100))
    assert store.claim_update_with_event(5, event_type="command_received", message="command=/status") is True
    assert store.claim_update_with_event(5, event_type="command_received", message="command=/status") is False
    assert store.get_last_update_id() == 5
    rows = store.list_events(limit=100)
    assert len(rows) == initial_count + 1
    assert rows[-1]["event_type"] == "command_received"
    assert rows[-1]["message"] == "command=/status"


def test_store_claim_update_with_event_respects_max_rows(store: Store) -> None:
    for i in range(MAX_EVENT_ROWS + 50):
        assert store.claim_update_with_event(i, event_type="claim", message=f"m{i}") is True
    rows = store.list_events(limit=MAX_EVENT_ROWS + 200)
    assert len(rows) == MAX_EVENT_ROWS
    assert rows[0]["message"] == "m50"


def test_reconcile_running_jobs_uses_pid_liveness(store: Store) -> None:
    job = store.create_job(command="run", prompt="keep running", status="RUNNING")
    store.set_job_pid(job.id, 43210, pid_start_token="token-43210")

    alive_summary = store.reconcile_running_jobs(
        pid_is_alive=lambda pid: pid == 43210,
        pid_start_token_matches=lambda pid, token: pid == 43210 and token == "token-43210",
    )
    assert alive_summary.recovered_count == 0
    assert alive_summary.orphan_running_count == 1
    active = store.get_job(job.id)
    assert active is not None
    assert active.status == "RUNNING"

    dead_summary = store.reconcile_running_jobs(
        pid_is_alive=lambda _pid: False,
        pid_start_token_matches=lambda _pid, _token: False,
    )
    assert dead_summary.recovered_count == 1
    assert dead_summary.orphan_running_count == 0
    recovered = store.get_job(job.id)
    assert recovered is not None
    assert recovered.status == "INTERRUPTED_RECOVERED"


def test_pid_start_token_matcher_defaults_true_when_token_missing() -> None:
    assert Store._pid_start_token_matches(1234, None) is True


@pytest.mark.skipif(os.name != "posix", reason="permission bits are POSIX-specific")
def test_store_hardens_file_permissions(settings, store: Store) -> None:  # noqa: ARG001
    db_mode = stat.S_IMODE(os.stat(settings.db_path).st_mode)
    audit_mode = stat.S_IMODE(os.stat(settings.audit_log_path).st_mode)
    db_dir_mode = stat.S_IMODE(os.stat(settings.db_path.parent).st_mode)
    audit_dir_mode = stat.S_IMODE(os.stat(settings.audit_log_path.parent).st_mode)

    assert db_mode & 0o077 == 0
    assert audit_mode & 0o077 == 0
    assert db_dir_mode & 0o077 == 0
    assert audit_dir_mode & 0o077 == 0


def test_confirmation_lifecycle(store: Store) -> None:
    confirmation = store.create_confirmation(
        nonce="abcd1234",
        command="autopilot",
        task="ship this",
        user_id=11,
        chat_id=22,
        ttl_seconds=60,
    )
    assert confirmation.nonce == "abcd1234"

    consumed = store.consume_confirmation("abcd1234", user_id=11, chat_id=22)
    assert consumed is not None
    assert consumed.task == "ship this"

    assert store.consume_confirmation("abcd1234", user_id=11, chat_id=22) is None


def test_create_confirmation_prunes_expired_entries(store: Store) -> None:
    expired = store.create_confirmation(
        nonce="expired1",
        command="run",
        task="stale",
        user_id=11,
        chat_id=22,
        ttl_seconds=-1,
    )
    assert expired.nonce == "expired1"

    fresh = store.create_confirmation(
        nonce="fresh1",
        command="run",
        task="fresh",
        user_id=11,
        chat_id=22,
        ttl_seconds=60,
    )
    assert fresh.nonce == "fresh1"
    assert store.get_confirmation("expired1", user_id=11, chat_id=22) is None


def test_store_prunes_events_to_max_rows(store: Store) -> None:
    for i in range(MAX_EVENT_ROWS + 25):
        store.add_event(None, "spam", f"event-{i}")

    rows = store.list_events(limit=MAX_EVENT_ROWS + 100)
    assert len(rows) == MAX_EVENT_ROWS
    assert rows[0]["message"] == "event-25"
    assert rows[-1]["message"] == f"event-{MAX_EVENT_ROWS + 24}"
