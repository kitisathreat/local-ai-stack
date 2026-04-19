"""Unit tests for backend/history_store.py — per-user AES-GCM append log."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("LAI_HISTORY_DIR", str(tmp_path))
    monkeypatch.setenv("AUTH_SECRET_KEY", "test-kek-do-not-use-in-prod")
    monkeypatch.delenv("HISTORY_SECRET_KEY", raising=False)
    import importlib
    from backend import history_store as hs
    importlib.reload(hs)
    return hs


def test_roundtrip_single_record(store, tmp_path):
    run(store.append(42, {"role": "user", "content": "hi"}))
    out = run(store.load_all(42))
    assert out == [{"role": "user", "content": "hi"}]
    # File exists and is non-empty.
    assert (tmp_path / "user_42.hist").stat().st_size > 0


def test_roundtrip_many_records(store):
    batch = [{"role": "user", "content": f"msg-{i}"} for i in range(5)]
    run(store.append_many(7, batch))
    run(store.append(7, {"role": "assistant", "content": "done"}))
    out = run(store.load_all(7))
    assert out[:5] == batch
    assert out[5] == {"role": "assistant", "content": "done"}


def test_users_are_isolated_by_key(store):
    run(store.append(1, {"role": "user", "content": "alice-secret"}))
    run(store.append(2, {"role": "user", "content": "bob-secret"}))
    # Each user reads only their own records.
    assert run(store.load_all(1)) == [{"role": "user", "content": "alice-secret"}]
    assert run(store.load_all(2)) == [{"role": "user", "content": "bob-secret"}]


def test_file_contents_are_encrypted(store, tmp_path):
    run(store.append(9, {"role": "user", "content": "cleartext-needle-xyz"}))
    blob = (tmp_path / "user_9.hist").read_bytes()
    assert b"cleartext-needle-xyz" not in blob
    # Header magic is still present.
    assert blob.startswith(b"LAIH")


def test_missing_file_returns_empty(store):
    assert run(store.load_all(404)) == []


def test_tamper_detection(store, tmp_path):
    run(store.append(11, {"role": "user", "content": "original"}))
    p = tmp_path / "user_11.hist"
    data = bytearray(p.read_bytes())
    # Flip a byte in the ciphertext region (past the 22-byte header).
    data[-1] ^= 0x01
    p.write_bytes(bytes(data))
    # Bad record is skipped, not raised.
    assert run(store.load_all(11)) == []


def test_wrong_key_cannot_read(store, tmp_path, monkeypatch):
    run(store.append(13, {"role": "user", "content": "locked"}))
    # Rotate the KEK — existing file is now garbage for decryption.
    monkeypatch.setenv("AUTH_SECRET_KEY", "different-kek-entirely")
    import importlib
    from backend import history_store as hs
    importlib.reload(hs)
    assert hs._load_all_sync(13) == []
