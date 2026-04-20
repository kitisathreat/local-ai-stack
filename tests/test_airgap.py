"""Tests for backend/airgap.py and the encryption/scoping it layers on
top of history_store."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def airgap_env(tmp_path, monkeypatch):
    """Isolated tmp-dir + KEK + fresh module imports. Returns a tuple
    (airgap_module, history_store_module, tmp_path)."""
    monkeypatch.setenv("LAI_HISTORY_DIR", str(tmp_path))
    monkeypatch.setenv("LAI_AIRGAP_STATE", str(tmp_path / "airgap.state"))
    monkeypatch.setenv("AUTH_SECRET_KEY", "test-kek-do-not-use-in-prod")
    monkeypatch.delenv("HISTORY_SECRET_KEY", raising=False)
    import importlib
    from backend import airgap, history_store as hs
    importlib.reload(hs)
    importlib.reload(airgap)
    return airgap, hs, tmp_path


def test_state_defaults_to_off(airgap_env):
    airgap, _, _ = airgap_env
    st = airgap.AirgapState()
    assert st.enabled is False
    assert airgap.is_enabled() is False


def test_state_persists_across_restarts(airgap_env):
    airgap, _, _ = airgap_env
    st = airgap.AirgapState()
    run(st.set(True, "ops@example.com"))
    # Fresh instance re-reads the file.
    st2 = airgap.AirgapState()
    assert st2.enabled is True
    assert st2.changed_by == "ops@example.com"


def test_is_enabled_module_singleton(airgap_env):
    airgap, _, _ = airgap_env
    st = airgap.AirgapState()
    airgap.set_current(st)
    assert airgap.is_enabled() is False
    run(st.set(True, None))
    assert airgap.is_enabled() is True
    run(st.set(False, None))
    assert airgap.is_enabled() is False


def test_airgap_and_normal_history_diverge(airgap_env):
    _, hs, tmp_path = airgap_env
    # Write to normal and airgap history for the same user; keys must be
    # independent and the files must live side-by-side.
    run(hs.append(5, {"role": "user", "content": "normal"}, airgap=False))
    run(hs.append(5, {"role": "user", "content": "airgap"}, airgap=True))

    normal = run(hs.load_all(5, airgap=False))
    airgap_records = run(hs.load_all(5, airgap=True))
    assert normal == [{"role": "user", "content": "normal"}]
    assert airgap_records == [{"role": "user", "content": "airgap"}]

    assert (tmp_path / "user_5.hist").exists()
    assert (tmp_path / "user_5.airgap.hist").exists()


def test_airgap_file_does_not_leak_to_normal_reader(airgap_env):
    _, hs, _ = airgap_env
    run(hs.append(6, {"role": "user", "content": "airgap-secret"}, airgap=True))
    # A normal-scope read must NOT see airgap records.
    assert run(hs.load_all(6, airgap=False)) == []


def test_encrypt_decrypt_roundtrip(airgap_env):
    _, hs, _ = airgap_env
    token = hs.encrypt_value(77, "hello world", scope="msg", airgap=True)
    assert hs.is_encrypted(token)
    assert "hello world" not in token
    assert hs.decrypt_value(77, token, scope="msg", airgap=True) == "hello world"


def test_encrypt_scope_isolation(airgap_env):
    """A token encrypted under scope=msg must not decrypt under scope=mem."""
    _, hs, _ = airgap_env
    token = hs.encrypt_value(3, "payload", scope="msg", airgap=True)
    # Wrong scope decrypts to "" (the decrypt helper logs + swallows).
    assert hs.decrypt_value(3, token, scope="mem", airgap=True) == ""


def test_encrypt_user_isolation(airgap_env):
    _, hs, _ = airgap_env
    token = hs.encrypt_value(100, "alice-only", scope="msg", airgap=True)
    assert hs.decrypt_value(101, token, scope="msg", airgap=True) == ""


def test_decrypt_passthrough_for_plaintext(airgap_env):
    _, hs, _ = airgap_env
    # Non-prefixed strings are returned unchanged so mixed-mode reads work.
    assert hs.decrypt_value(1, "plain text", scope="msg", airgap=True) == "plain text"
    assert hs.decrypt_value(1, "", scope="msg", airgap=True) == ""
