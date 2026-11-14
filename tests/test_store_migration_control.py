"""Migration controls, safe command output and thread ownership without a database."""

import asyncio
import logging
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from contextvars import copy_context
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gateway.settings import Settings
from gateway.store import create_store
from gateway.store.postgres import PostgresStore
from scripts import migrate_db

INVENTED_URL = "postgresql://invented:invented-password@invented.invalid/invented"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        ("false", False),
        ("true", True),
        ("1", True),
        ("yes", True),
        ("TRUE", True),
        ("YES", True),
        ("0", False),
        ("on", False),
        (" true ", False),
    ],
)
def test_migrate_on_start_parsing(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: bool
) -> None:
    monkeypatch.delenv("MIGRATE_ON_START", raising=False)
    if value is not None:
        monkeypatch.setenv("MIGRATE_ON_START", value)
    assert Settings().migrate_on_start is expected


@pytest.mark.parametrize("migrate", [False, True])
def test_factory_passes_migration_flag(monkeypatch: pytest.MonkeyPatch, migrate: bool) -> None:
    constructor = MagicMock()
    monkeypatch.setattr("gateway.store.postgres.PostgresStore", constructor)
    result = create_store(
        Settings(store="postgres", database_url=INVENTED_URL, migrate_on_start=migrate)
    )
    constructor.assert_called_once_with(INVENTED_URL, migrate=migrate)
    assert result is constructor.return_value


def test_migration_command_missing_url_has_no_traceback() -> None:
    environment = os.environ.copy()
    environment.pop("DATABASE_URL", None)
    result = subprocess.run(
        [sys.executable, "-m", "scripts.migrate_db"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout.strip() == "Set DATABASE_URL before running migrations."
    assert result.stderr == ""


@pytest.mark.parametrize("failure", [None, "connect", "migrate", "close"])
def test_migration_command_output_is_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    failure: str | None,
) -> None:
    monkeypatch.setenv("DATABASE_URL", INVENTED_URL)
    store = MagicMock()
    store.migrate.return_value = ["001_init.sql", "002_approval_guards.sql"]
    if failure in {"migrate", "close"}:
        getattr(store, failure).side_effect = RuntimeError(INVENTED_URL)

    def connect(url: str, *, migrate: bool) -> MagicMock:
        assert url == INVENTED_URL and migrate is False
        logging.getLogger("psycopg.pool").warning("Connection failed: %s", url)
        if failure == "connect":
            raise RuntimeError(url)
        return store

    monkeypatch.setattr(migrate_db, "PostgresStore", connect)
    logger = logging.getLogger("psycopg.pool")
    was_disabled = logger.disabled
    assert migrate_db.main() == (0 if failure is None else 1)
    output = capsys.readouterr()
    assert INVENTED_URL not in output.out + output.err + caplog.text
    assert "Traceback" not in output.out + output.err
    assert output.err == ""
    assert logger.disabled == was_disabled
    if failure is None:
        assert output.out == "001_init.sql\n002_approval_guards.sql\ndone\n"
    else:
        assert output.out == (
            "Database migration failed. Check the configuration and connection.\n"
        )
    if failure != "connect":
        store.close.assert_called_once_with()


@pytest.fixture
def offline_store(monkeypatch: pytest.MonkeyPatch) -> tuple[PostgresStore, MagicMock]:
    conn = MagicMock()
    conn.transaction.side_effect = lambda: nullcontext()
    pool = MagicMock()
    pool.connection.side_effect = lambda: nullcontext(conn)
    monkeypatch.setattr("gateway.store.postgres.ConnectionPool", MagicMock(return_value=pool))
    return PostgresStore(INVENTED_URL, migrate=False), conn


def test_migrations_run_sorted_in_one_transaction(
    offline_store: tuple[PostgresStore, MagicMock], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, conn = offline_store
    conn.transaction.assert_not_called()
    conn.execute.assert_not_called()
    (tmp_path / "002_second.sql").write_text("SELECT 2", encoding="utf-8")
    (tmp_path / "001_first.sql").write_text("SELECT 1", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("ignored", encoding="utf-8")
    monkeypatch.setattr("gateway.store.postgres.MIGRATIONS", tmp_path)
    assert store.migrate() == ["001_first.sql", "002_second.sql"]
    conn.transaction.assert_called_once_with()
    calls = conn.execute.call_args_list
    assert calls[0].args[0] == "SELECT pg_advisory_xact_lock(%s)"
    assert [call.args[0].as_string() for call in calls[1:]] == ["SELECT 1", "SELECT 2"]


@pytest.mark.parametrize("operation", ["transaction", "read", "write"])
def test_copied_transaction_rejects_another_thread(
    offline_store: tuple[PostgresStore, MagicMock], operation: str
) -> None:
    store, conn = offline_store

    def inherited_operation() -> None:
        with pytest.raises(RuntimeError, match="transaction belongs to another thread"):
            if operation == "transaction":
                with store.transaction():
                    pytest.fail("Inherited transaction must not be entered")
            elif operation == "read":
                with store._conn():
                    pytest.fail("Inherited connection must not be shared")
            else:
                store._write()

    with ThreadPoolExecutor(max_workers=1) as executor, store.transaction():
        context = copy_context()
        executor.submit(context.run, inherited_operation).result(timeout=5)
        with store.transaction(), store._conn() as nested:
            assert nested is conn and store._write() is conn
    conn.transaction.assert_called_once_with()


async def test_to_thread_rejects_inherited_transaction(
    offline_store: tuple[PostgresStore, MagicMock],
) -> None:
    store, _ = offline_store

    def enter_transaction() -> None:
        with store.transaction():
            pytest.fail("Inherited transaction must not be entered")

    with store.transaction():
        with pytest.raises(RuntimeError, match="transaction belongs to another thread"):
            await asyncio.to_thread(enter_transaction)
