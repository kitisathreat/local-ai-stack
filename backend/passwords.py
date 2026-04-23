"""Password hashing + verification.

bcrypt with a tunable cost factor. ``BCRYPT_ROUNDS`` env var overrides
the default of 12; lower for CI, leave at 12 for interactive deploys.

Stores the hash as a utf-8 string (bcrypt's native output format) so it
round-trips through SQLite TEXT columns without base64.
"""
from __future__ import annotations

import os

import bcrypt


def _rounds() -> int:
    raw = os.getenv("BCRYPT_ROUNDS")
    if not raw:
        return 12
    try:
        n = int(raw)
    except ValueError:
        return 12
    return max(4, min(n, 16))


def hash_password(plain: str) -> str:
    if not plain:
        raise ValueError("password may not be empty")
    salt = bcrypt.gensalt(rounds=_rounds())
    return bcrypt.hashpw(plain.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        # hash not in bcrypt format
        return False
