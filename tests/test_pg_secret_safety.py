"""The Postgres fixture must never print the connection string, which holds a password."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import PgTarget

ROOT = Path(__file__).resolve().parents[1]
# Invented credentials; port 1 on localhost refuses at once.
FAKE_PASSWORD = "invented-pg-password-7Qx2"
FAKE_URL = f"postgresql://invented_user:{FAKE_PASSWORD}@127.0.0.1:1/invented_db?connect_timeout=1"


def run_store_tests(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    child_env = {
        k: v for k, v in os.environ.items() if k not in {"DATABASE_URL", "LIMOS_SKIP_PG_TESTS"}
    }
    child_env.update(env)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_store_postgres.py::test_decision_log_cannot_be_edited_deleted_or_truncated",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--basetemp={tmp_path / 'inner'}",
        ],
        cwd=ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_repr_hides_the_url() -> None:
    target = PgTarget(FAKE_URL, "limos_test_x")
    assert FAKE_PASSWORD not in repr(target)
    assert FAKE_PASSWORD not in str(target)
    url, schema = target
    assert url == FAKE_URL and schema == "limos_test_x"


def test_connection_failure_never_prints_the_password(tmp_path: Path) -> None:
    pytest.importorskip("psycopg")
    result = run_store_tests(tmp_path, DATABASE_URL=FAKE_URL)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "could not connect or create the test schema (details hidden)" in output
    assert FAKE_PASSWORD not in output
    assert "invented_user" not in output


def test_skip_variable_skips_without_a_connection(tmp_path: Path) -> None:
    result = run_store_tests(tmp_path, DATABASE_URL=FAKE_URL, LIMOS_SKIP_PG_TESTS="1")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-2000:]
    assert " skipped" in output and " failed" not in output
    assert FAKE_PASSWORD not in output
