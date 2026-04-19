"""Encrypted per-user chat-history log.

Every user has one append-only file at `$LAI_HISTORY_DIR/user_{id}.hist`
(defaults to `/app/data/history/`). Each chat message the user opts into
persisting (see `conversations.memory_enabled`) is appended as a length-
prefixed AES-256-GCM record.

Cryptographic design (v1):

    Key material
        A single server-side "key-encryption key" (KEK) is loaded from
        HISTORY_SECRET_KEY, falling back to AUTH_SECRET_KEY so single-
        tenant deployments don't have to configure two secrets. Rotating
        AUTH_SECRET_KEY without setting HISTORY_SECRET_KEY will make any
        existing history files unreadable — document that tradeoff.

    Per-user data key
        HKDF-SHA256(ikm=KEK, salt=per-file 16B random, info=f"lai-hist-v1:user:{user_id}")
        → 32-byte AES-256 key. The salt is written once at file creation
        time and read back on open.

    Per-record AEAD
        AES-256-GCM with a fresh 96-bit random nonce per record. The
        authenticated-associated data (AAD) binds each record to the
        owning user_id + file version so a record can't be transplanted
        across users or downgraded. GCM's 96-bit random nonce is safe up
        to ~2^32 records per key; realistic message volumes (millions per
        user) stay comfortably within bound.

    File format
        [4B magic "LAIH"][1B version=1][1B reserved=0][16B salt]
        then a sequence of records:
        [4B BE length N][12B nonce][N bytes ciphertext||16B tag]

        Records are plain JSON lines before encryption — so `load_all`
        streams them back into dicts without a second schema.

    Why AES-GCM over, e.g., fernet/ChaCha20
        - AEAD (integrity + confidentiality in one pass).
        - Hardware-accelerated on any CPU a backend container will run on.
        - Available in the stdlib-grade `cryptography` package already.
        XChaCha20-Poly1305 (PyNaCl) is a fine alternative with a 192-bit
        nonce (no collision risk at all) — if you want it, swap `AESGCM`
        for `nacl.secret.Aead` in `_encrypt`/`_decrypt` below. The format
        is otherwise identical.

    Threat model
        - An attacker with *only* the file cannot read or tamper with
          records (confidentiality + integrity).
        - An attacker with the KEK can decrypt every user's history. Treat
          AUTH_SECRET_KEY / HISTORY_SECRET_KEY like a database password.
        - An attacker with the running FastAPI process can read anything
          the process can read. This store is about disk-at-rest, not
          compromise-at-runtime.

The module is deliberately self-contained and sync at the file layer
— file I/O is tiny (a few KB per append) and wrapping in `asyncio.to_thread`
avoids an async-file-IO dep while keeping the event loop unblocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import struct
from pathlib import Path
from typing import Any, Iterable

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


logger = logging.getLogger(__name__)


_MAGIC = b"LAIH"
_VERSION = 1
_HEADER_LEN = 4 + 1 + 1 + 16          # magic + version + reserved + salt
_NONCE_LEN = 12
_TAG_LEN = 16


def _history_dir() -> Path:
    return Path(os.getenv("LAI_HISTORY_DIR", "/app/data/history"))


def _kek() -> bytes:
    """Return the server-side key-encryption key as raw bytes."""
    key = os.getenv("HISTORY_SECRET_KEY") or os.getenv("AUTH_SECRET_KEY")
    if not key:
        raise RuntimeError(
            "HISTORY_SECRET_KEY (or AUTH_SECRET_KEY) must be set before "
            "writing encrypted chat history."
        )
    # The auth module already generates AUTH_SECRET_KEY as urlsafe base64
    # text. We treat it as opaque bytes here; HKDF accepts any length.
    return key.encode("utf-8")


def _derive_key(salt: bytes, user_id: int) -> bytes:
    info = f"lai-hist-v{_VERSION}:user:{user_id}".encode("ascii")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=info,
    ).derive(_kek())


def _path(user_id: int) -> Path:
    return _history_dir() / f"user_{user_id}.hist"


def _read_header(path: Path) -> bytes:
    """Return the 16-byte salt from an existing file, or raise ValueError."""
    with path.open("rb") as f:
        hdr = f.read(_HEADER_LEN)
    if len(hdr) != _HEADER_LEN or hdr[:4] != _MAGIC:
        raise ValueError(f"history file {path} has invalid header")
    version = hdr[4]
    if version != _VERSION:
        raise ValueError(f"history file {path} has unsupported version {version}")
    return hdr[6:6 + 16]


def _open_or_create(user_id: int) -> tuple[Path, bytes]:
    """Ensure the user's history file exists and return (path, salt)."""
    path = _path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size >= _HEADER_LEN:
        salt = _read_header(path)
        return path, salt
    salt = secrets.token_bytes(16)
    hdr = _MAGIC + bytes([_VERSION, 0]) + salt
    # Write atomically: file either has a full header or doesn't exist yet.
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as f:
        f.write(hdr)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    # Best-effort: tighten perms so other containerized tenants can't read.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path, salt


def _aad(user_id: int) -> bytes:
    return f"lai-hist-v{_VERSION}|user:{user_id}".encode("ascii")


def _append_record_sync(user_id: int, payload: dict[str, Any]) -> None:
    path, salt = _open_or_create(user_id)
    key = _derive_key(salt, user_id)
    aead = AESGCM(key)
    plaintext = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    nonce = secrets.token_bytes(_NONCE_LEN)
    ct = aead.encrypt(nonce, plaintext, _aad(user_id))
    # Record payload bytes = nonce || ct(+tag)
    body = nonce + ct
    length = struct.pack(">I", len(body))
    with path.open("ab") as f:
        f.write(length)
        f.write(body)
        f.flush()
        os.fsync(f.fileno())


def _load_all_sync(user_id: int) -> list[dict[str, Any]]:
    path = _path(user_id)
    if not path.exists() or path.stat().st_size < _HEADER_LEN:
        return []
    salt = _read_header(path)
    key = _derive_key(salt, user_id)
    aead = AESGCM(key)
    aad = _aad(user_id)
    out: list[dict[str, Any]] = []
    with path.open("rb") as f:
        f.seek(_HEADER_LEN)
        while True:
            head = f.read(4)
            if not head:
                break
            if len(head) < 4:
                logger.warning("history for user %d truncated at length prefix", user_id)
                break
            (length,) = struct.unpack(">I", head)
            if length < _NONCE_LEN + _TAG_LEN:
                logger.warning("history for user %d has malformed record length", user_id)
                break
            body = f.read(length)
            if len(body) < length:
                logger.warning("history for user %d truncated mid-record", user_id)
                break
            nonce, ct = body[:_NONCE_LEN], body[_NONCE_LEN:]
            try:
                plaintext = aead.decrypt(nonce, ct, aad)
            except Exception as e:
                # Don't halt on a single bad record — surface and skip.
                logger.warning("history for user %d: record failed auth: %s", user_id, e)
                continue
            try:
                out.append(json.loads(plaintext.decode("utf-8")))
            except Exception:
                logger.warning("history for user %d: record has invalid JSON", user_id)
    return out


# ── Async wrappers ───────────────────────────────────────────────────────

async def append(user_id: int, record: dict[str, Any]) -> None:
    """Encrypt + append a single record. Never raises; logs on failure."""
    try:
        await asyncio.to_thread(_append_record_sync, user_id, record)
    except Exception:
        logger.exception("Failed to append history record for user %d", user_id)


async def append_many(user_id: int, records: Iterable[dict[str, Any]]) -> None:
    recs = list(records)
    if not recs:
        return
    try:
        await asyncio.to_thread(_append_many_sync, user_id, recs)
    except Exception:
        logger.exception("Failed to append history batch for user %d", user_id)


def _append_many_sync(user_id: int, records: list[dict[str, Any]]) -> None:
    # Single file-open for a batch — cheaper than N separate appends when
    # a turn persists multiple rows (user msg + assistant reply).
    path, salt = _open_or_create(user_id)
    key = _derive_key(salt, user_id)
    aead = AESGCM(key)
    aad = _aad(user_id)
    with path.open("ab") as f:
        for payload in records:
            plaintext = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            nonce = secrets.token_bytes(_NONCE_LEN)
            ct = aead.encrypt(nonce, plaintext, aad)
            body = nonce + ct
            f.write(struct.pack(">I", len(body)))
            f.write(body)
        f.flush()
        os.fsync(f.fileno())


async def load_all(user_id: int) -> list[dict[str, Any]]:
    """Decrypt and return every record for a user, in append order."""
    return await asyncio.to_thread(_load_all_sync, user_id)


async def file_size(user_id: int) -> int:
    p = _path(user_id)
    try:
        return p.stat().st_size
    except OSError:
        return 0
