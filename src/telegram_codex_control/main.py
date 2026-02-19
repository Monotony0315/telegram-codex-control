from __future__ import annotations

import asyncio
import os

from .bot import TelegramBotDaemon
from .config import Settings
from .runner import Runner
from .safety import SafetyManager
from .store import Store


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


async def run_daemon() -> None:
    settings = Settings.from_env()
    store = Store(settings.db_path, settings.audit_log_path)
    store.initialize()

    recovery = store.reconcile_running_jobs(pid_is_alive=_pid_is_alive)
    if recovery.recovered_count:
        store.add_event(
            None,
            "recovery",
            f"Recovered {recovery.recovered_count} interrupted jobs",
        )
    if recovery.orphan_running_count:
        store.add_event(
            None,
            "recovery_orphan",
            (
                f"Detected {recovery.orphan_running_count} orphan RUNNING jobs with live PIDs; "
                "use /cancel to terminate them"
            ),
        )

    runner = Runner(settings, store)
    safety = SafetyManager(store, confirmation_ttl_seconds=settings.confirmation_ttl_seconds)
    bot = TelegramBotDaemon(settings, store, runner, safety)

    try:
        await bot.run_forever()
    finally:
        await bot.close()
        store.close()


def main() -> None:
    try:
        asyncio.run(run_daemon())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
