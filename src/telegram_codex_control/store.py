from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import threading

from .utils import redact_text, utc_now_iso


ACTIVE_STATUSES = ("PENDING", "CONFIRMING", "RUNNING")
TERMINAL_STATUSES = (
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "TIMED_OUT",
    "INTERRUPTED_RECOVERED",
)
MAX_EVENT_ROWS = 5000
MAX_AUDIT_LOG_BYTES = 5 * 1024 * 1024


class ActiveJobExistsError(RuntimeError):
    """Raised when attempting to create a second active job."""


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    command: str
    prompt: str
    status: str
    created_at: str
    updated_at: str
    started_at: str | None
    ended_at: str | None
    pid: int | None
    pid_start_token: str | None
    exit_code: int | None
    error: str | None


@dataclass(frozen=True, slots=True)
class Confirmation:
    nonce: str
    command: str
    task: str
    user_id: int
    chat_id: int
    created_at: str
    expires_at: str
    consumed_at: str | None


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    recovered_count: int
    orphan_running_count: int


class Store:
    def __init__(self, db_path: Path, audit_log_path: Path):
        self.db_path = Path(db_path)
        self.audit_log_path = Path(audit_log_path)
        self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.audit_log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._chmod_if_exists(self.db_path.parent, 0o700)
        self._chmod_if_exists(self.audit_log_path.parent, 0o700)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=20.0)
        self._conn.row_factory = sqlite3.Row
        self._chmod_if_exists(self.db_path, 0o600)
        self.audit_log_path.touch(exist_ok=True)
        self._chmod_if_exists(self.audit_log_path, 0o600)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def initialize(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON;")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    ended_at TEXT,
                    pid INTEGER,
                    pid_start_token TEXT,
                    exit_code INTEGER,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (job_id) REFERENCES jobs(id)
                );

                CREATE TABLE IF NOT EXISTS offsets (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_update_id INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pending_confirmations (
                    nonce TEXT PRIMARY KEY,
                    command TEXT NOT NULL,
                    task TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_single_active
                ON jobs((1))
                WHERE status IN ('PENDING', 'CONFIRMING', 'RUNNING');
                """
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO offsets (id, last_update_id) VALUES (1, -1);"
            )
            self._ensure_column_exists("jobs", "pid_start_token", "TEXT")
            self._conn.commit()

    def claim_update(self, update_id: int) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE offsets
                SET last_update_id = ?
                WHERE id = 1 AND last_update_id < ?
                """,
                (update_id, update_id),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def claim_update_with_event(
        self,
        update_id: int,
        *,
        event_type: str,
        message: str,
        job_id: int | None = None,
    ) -> bool:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._conn.execute(
                    """
                    UPDATE offsets
                    SET last_update_id = ?
                    WHERE id = 1 AND last_update_id < ?
                    """,
                    (update_id, update_id),
                )
                if cursor.rowcount != 1:
                    self._conn.rollback()
                    return False

                self._conn.execute(
                    """
                    INSERT INTO events (job_id, event_type, message, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (job_id, event_type, message, now),
                )
                self._conn.execute(
                    """
                    DELETE FROM events
                    WHERE id NOT IN (
                        SELECT id FROM events ORDER BY id DESC LIMIT ?
                    )
                    """,
                    (MAX_EVENT_ROWS,),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            self._append_audit_line(
                {
                    "created_at": now,
                    "job_id": job_id,
                    "event_type": event_type,
                    "message": message,
                    "update_id": update_id,
                }
            )
            return True

    def get_last_update_id(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_update_id FROM offsets WHERE id = 1;"
            ).fetchone()
            return int(row["last_update_id"]) if row else -1

    def create_job(self, command: str, prompt: str, status: str = "RUNNING") -> Job:
        now = utc_now_iso()
        started_at = now if status == "RUNNING" else None
        stored_prompt = redact_text(prompt)
        with self._lock:
            try:
                cursor = self._conn.execute(
                    """
                    INSERT INTO jobs (command, prompt, status, created_at, updated_at, started_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (command, stored_prompt, status, now, now, started_at),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise ActiveJobExistsError("An active job already exists") from exc
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (cursor.lastrowid,)).fetchone()
            assert row is not None
            return self._row_to_job(row)

    def set_job_pid(self, job_id: int, pid: int | None, *, pid_start_token: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET pid = ?, pid_start_token = ?, updated_at = ? WHERE id = ?",
                (pid, pid_start_token, utc_now_iso(), job_id),
            )
            self._conn.commit()

    def set_job_status(
        self,
        job_id: int,
        status: str,
        *,
        exit_code: int | None = None,
        error: str | None = None,
    ) -> None:
        now = utc_now_iso()
        ended_at = now if status in TERMINAL_STATUSES else None
        with self._lock:
            self._conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    updated_at = ?,
                    ended_at = COALESCE(?, ended_at),
                    exit_code = ?,
                    error = ?
                WHERE id = ?
                """,
                (status, now, ended_at, exit_code, error, job_id),
            )
            self._conn.commit()

    def get_job(self, job_id: int) -> Job | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_job(row) if row else None

    def get_active_job(self) -> Job | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('PENDING', 'CONFIRMING', 'RUNNING')
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            return self._row_to_job(row) if row else None

    def recover_interrupted_jobs(self) -> int:
        summary = self.reconcile_running_jobs(pid_is_alive=lambda _pid: False)
        return summary.recovered_count

    def reconcile_running_jobs(
        self,
        *,
        pid_is_alive: Callable[[int], bool],
        pid_start_token_matches: Callable[[int, str | None], bool] | None = None,
    ) -> RecoverySummary:
        token_matcher = self._pid_start_token_matches if pid_start_token_matches is None else pid_start_token_matches
        with self._lock:
            now = utc_now_iso()
            running_rows = self._conn.execute(
                "SELECT id, pid, pid_start_token, error FROM jobs WHERE status = 'RUNNING' ORDER BY id DESC"
            ).fetchall()

            recovered_count = 0
            orphan_running_count = 0

            for row in running_rows:
                pid = row["pid"]
                pid_start_token = row["pid_start_token"]
                if (
                    isinstance(pid, int)
                    and pid > 0
                    and pid_is_alive(pid)
                    and token_matcher(pid, pid_start_token)
                ):
                    orphan_running_count += 1
                    self._conn.execute(
                        """
                        UPDATE jobs
                        SET updated_at = ?,
                            error = COALESCE(error, 'Recovered after restart; process still alive')
                        WHERE id = ?
                        """,
                        (now, row["id"]),
                    )
                    continue

                recovered_count += 1
                self._conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'INTERRUPTED_RECOVERED',
                        updated_at = ?,
                        ended_at = ?,
                        error = COALESCE(error, 'Recovered after restart while job process was not alive')
                    WHERE id = ?
                    """,
                    (now, now, row["id"]),
                )

            self._conn.commit()
            return RecoverySummary(
                recovered_count=recovered_count,
                orphan_running_count=orphan_running_count,
            )

    def add_event(self, job_id: int | None, event_type: str, message: str) -> None:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO events (job_id, event_type, message, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (job_id, event_type, message, now),
            )
            self._conn.execute(
                """
                DELETE FROM events
                WHERE id NOT IN (
                    SELECT id FROM events ORDER BY id DESC LIMIT ?
                )
                """,
                (MAX_EVENT_ROWS,),
            )
            self._conn.commit()
            self._append_audit_line(
                {
                    "created_at": now,
                    "job_id": job_id,
                    "event_type": event_type,
                    "message": message,
                }
            )

    def list_events(self, limit: int = 100) -> list[sqlite3.Row]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return list(reversed(rows))

    def create_confirmation(
        self,
        *,
        nonce: str,
        command: str,
        task: str,
        user_id: int,
        chat_id: int,
        ttl_seconds: int,
    ) -> Confirmation:
        created_at = utc_now_iso()
        expires_at = utc_now_iso_from_seconds(ttl_seconds)
        with self._lock:
            self._conn.execute(
                """
                DELETE FROM pending_confirmations
                WHERE consumed_at IS NULL AND expires_at <= ?
                """,
                (created_at,),
            )
            self._conn.execute(
                """
                INSERT INTO pending_confirmations
                (nonce, command, task, user_id, chat_id, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (nonce, command, task, user_id, chat_id, created_at, expires_at),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM pending_confirmations WHERE nonce = ?",
                (nonce,),
            ).fetchone()
            assert row is not None
            return self._row_to_confirmation(row)

    def get_confirmation(
        self,
        nonce: str,
        *,
        user_id: int,
        chat_id: int,
    ) -> Confirmation | None:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                DELETE FROM pending_confirmations
                WHERE consumed_at IS NULL AND expires_at <= ?
                """,
                (now,),
            )
            row = self._conn.execute(
                """
                SELECT * FROM pending_confirmations
                WHERE nonce = ?
                  AND user_id = ?
                  AND chat_id = ?
                  AND consumed_at IS NULL
                  AND expires_at > ?
                """,
                (nonce, user_id, chat_id, now),
            ).fetchone()
            self._conn.commit()
            if not row:
                return None
            return self._row_to_confirmation(row)

    def consume_confirmation(
        self,
        nonce: str,
        *,
        user_id: int,
        chat_id: int,
    ) -> Confirmation | None:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                DELETE FROM pending_confirmations
                WHERE consumed_at IS NULL AND expires_at <= ?
                """,
                (now,),
            )
            row = self._conn.execute(
                """
                SELECT * FROM pending_confirmations
                WHERE nonce = ?
                  AND user_id = ?
                  AND chat_id = ?
                  AND consumed_at IS NULL
                  AND expires_at > ?
                """,
                (nonce, user_id, chat_id, now),
            ).fetchone()
            if not row:
                self._conn.commit()
                return None
            self._conn.execute(
                "UPDATE pending_confirmations SET consumed_at = ? WHERE nonce = ?",
                (now, nonce),
            )
            self._conn.commit()
            return Confirmation(
                nonce=row["nonce"],
                command=row["command"],
                task=row["task"],
                user_id=row["user_id"],
                chat_id=row["chat_id"],
                created_at=row["created_at"],
                expires_at=row["expires_at"],
                consumed_at=now,
            )

    def _append_audit_line(self, payload: dict) -> None:
        try:
            if self.audit_log_path.exists() and self.audit_log_path.stat().st_size > MAX_AUDIT_LOG_BYTES:
                rotated = self.audit_log_path.with_suffix(self.audit_log_path.suffix + ".1")
                if rotated.exists():
                    rotated.unlink()
                self.audit_log_path.replace(rotated)
            with self.audit_log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
            self._chmod_if_exists(self.audit_log_path, 0o600)
        except OSError:
            # Audit sink is best-effort and must not crash control-plane flow.
            return

    @staticmethod
    def _chmod_if_exists(path: Path, mode: int) -> None:
        try:
            os.chmod(path, mode)
        except OSError:
            return

    def _ensure_column_exists(self, table_name: str, column_name: str, column_type: str) -> None:
        columns = self._conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        if any(column["name"] == column_name for column in columns):
            return
        self._conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")

    @staticmethod
    def _pid_start_token_matches(pid: int, expected_token: str | None) -> bool:
        if not expected_token:
            # Legacy rows may not have a token. Be conservative and keep RUNNING guard.
            return True
        try:
            output = subprocess.check_output(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            return False
        if not output:
            return False
        return output == expected_token

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            command=row["command"],
            prompt=row["prompt"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            pid=row["pid"],
            pid_start_token=row["pid_start_token"],
            exit_code=row["exit_code"],
            error=row["error"],
        )

    @staticmethod
    def _row_to_confirmation(row: sqlite3.Row) -> Confirmation:
        return Confirmation(
            nonce=row["nonce"],
            command=row["command"],
            task=row["task"],
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            consumed_at=row["consumed_at"],
        )


def utc_now_iso_from_seconds(seconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
