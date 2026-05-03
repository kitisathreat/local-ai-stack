"""Shared pytest fixtures for the backend test suite.

Most existing tests duplicated the same monkeypatch incantations:

  - `LAI_DATA_DIR` pointed at a tmp_path (12 occurrences)
  - `BCRYPT_ROUNDS=4` to keep bcrypt fast in CI (6 occurrences)
  - `AUTH_SECRET_KEY` / `HISTORY_SECRET_KEY` set to a valid base64
    32-byte secret (5 occurrences)
  - The `good_secret()` helper (defined inline in test_diagnostics)

This conftest centralizes those patterns so new tests don't have to
reinvent the setup, and existing tests can opt into them as they're
touched.
"""

from __future__ import annotations

import base64

import pytest


def good_secret() -> str:
    """Valid URL-safe base64 encoding of 32 bytes — what AUTH_SECRET_KEY
    and HISTORY_SECRET_KEY expect at startup. Re-exported so tests can
    `from conftest import good_secret` if they're run as a script, or
    use the `good_secret_value` fixture below in pytest mode.
    """
    return base64.urlsafe_b64encode(b"a" * 32).decode()


@pytest.fixture
def good_secret_value() -> str:
    """A valid AUTH_SECRET_KEY / HISTORY_SECRET_KEY value (base64-encoded
    32 bytes). Use as a literal — does NOT call setenv for you, since
    not every test wants both vars set."""
    return good_secret()


@pytest.fixture
def fast_bcrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set `BCRYPT_ROUNDS=4` — bcrypt's minimum, ~30× faster than the
    production default of 12. Use in any test that hashes a password
    so CI doesn't spend seconds-per-test on cost factor."""
    monkeypatch.setenv("BCRYPT_ROUNDS", "4")


@pytest.fixture
def lai_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path) -> "pytest.TempPathFactory":
    """Point `LAI_DATA_DIR` at a per-test tmp directory. Backend modules
    (db, history_store, rag, memory, model_resolver) all read this env
    var as their root for SQLite, encrypted history, the model cache,
    etc. Returns the tmp_path so the test can also touch files inside."""
    monkeypatch.setenv("LAI_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def auth_env(
    monkeypatch: pytest.MonkeyPatch,
    good_secret_value: str,
) -> None:
    """The minimal env contract the backend's AppConfig validates at
    startup: AUTH_SECRET_KEY + HISTORY_SECRET_KEY both set to a valid
    base64-encoded 32-byte secret, plus a chat hostname."""
    monkeypatch.setenv("AUTH_SECRET_KEY", good_secret_value)
    monkeypatch.setenv("HISTORY_SECRET_KEY", good_secret_value)
    monkeypatch.setenv("CHAT_HOSTNAME", "localhost")
