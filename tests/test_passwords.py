"""Tests for backend.passwords — bcrypt hash + verify.

The module is small (~40 lines) but security-critical. These tests pin
the contract: hashes round-trip, empty inputs reject, malformed hashes
verify-False instead of raising, and the BCRYPT_ROUNDS env override
clamps to a sensible range.
"""

from __future__ import annotations

import pytest

from backend.passwords import _rounds, hash_password, verify_password


def test_hash_then_verify_succeeds(fast_bcrypt: None) -> None:
    h = hash_password("hunter2")
    assert verify_password("hunter2", h) is True


def test_verify_wrong_password_returns_false(fast_bcrypt: None) -> None:
    h = hash_password("right")
    assert verify_password("wrong", h) is False


def test_hash_returns_bcrypt_format(fast_bcrypt: None) -> None:
    """bcrypt's $2b$ / $2a$ / $2y$ prefix indicates the salt scheme.
    The current bcrypt library produces $2b$. Asserting the prefix
    catches accidental introduction of a different scheme (e.g.
    a mistaken hashlib drop-in)."""
    h = hash_password("anything")
    assert h.startswith("$2")
    # bcrypt returns a 60-char hash. Anything else is suspicious.
    assert len(h) == 60


def test_hash_is_stable_round_trip_through_str(fast_bcrypt: None) -> None:
    """The hash is stored as a TEXT column in SQLite, so the str
    round-trip must preserve every byte. bcrypt encodes salt + cost +
    digest in ASCII so this should be a no-op, but let's make it a
    hard assertion."""
    h = hash_password("password123")
    assert verify_password("password123", str(h)) is True


def test_empty_password_raises_on_hash() -> None:
    with pytest.raises(ValueError):
        hash_password("")


def test_verify_with_empty_password_returns_false(fast_bcrypt: None) -> None:
    h = hash_password("not empty")
    assert verify_password("", h) is False


def test_verify_with_empty_hash_returns_false() -> None:
    """Empty hash short-circuits to False rather than raising — important
    for "first-run, no admin yet" code paths that may stub a None hash."""
    assert verify_password("anything", "") is False


def test_verify_with_garbage_hash_returns_false() -> None:
    """A hash that's not bcrypt-formatted should verify-False, not
    raise. This protects against a corrupted DB column tanking the
    login endpoint."""
    assert verify_password("anything", "not-a-bcrypt-hash") is False


def test_rounds_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BCRYPT_ROUNDS", raising=False)
    assert _rounds() == 12


def test_rounds_clamps_below_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BCRYPT_ROUNDS", "1")
    # bcrypt's library minimum is 4
    assert _rounds() == 4


def test_rounds_clamps_above_maximum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BCRYPT_ROUNDS", "100")
    # We cap at 16 so a typo can't lock the server in a 30-minute
    # password verification.
    assert _rounds() == 16


def test_rounds_falls_back_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BCRYPT_ROUNDS", "not-an-int")
    assert _rounds() == 12
