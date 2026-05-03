"""Tests for backend.error_codes — stable LAI-* identifiers + helpers.

The module is mostly data (one ErrorCode per error scenario). Only
two functions have behavior worth pinning:

  - raise_with_code: builds the HTTPException body + header
  - format_error: stringifies for non-HTTP error paths

These tests also catch accidental code-id collisions and ensure the
HTTP-status mapping doesn't drift away from the documented norms.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from backend.error_codes import (
    AUTH_INVALID_CREDS,
    CFG_AUTH_SECRET_MISSING,
    INTERNAL_UNEXPECTED,
    RATE_PER_USER_DAY,
    RATE_PER_USER_MINUTE,
    TOOL_INVALID_ARGS,
    VRAM_EXHAUSTED,
    ErrorCode,
    format_error,
    raise_with_code,
)
from backend import error_codes as ec


def _all_codes() -> list[ErrorCode]:
    """Every ErrorCode constant declared at module level."""
    return [v for v in vars(ec).values() if isinstance(v, ErrorCode)]


def test_codes_are_unique() -> None:
    codes = [c.code for c in _all_codes()]
    assert len(codes) == len(set(codes)), "Duplicate LAI-* identifier(s)"


def test_codes_follow_format() -> None:
    """LAI-<SUBSYSTEM>-<3-digit>. Catches typos that would silently
    break log-aggregation regexes."""
    import re
    pattern = re.compile(r"^LAI-[A-Z]+-\d{3}$")
    for c in _all_codes():
        assert pattern.match(c.code), f"Bad format: {c.code}"


def test_http_status_mappings() -> None:
    """The module docstring documents the family-to-status convention:
    AUTH=401/403, RATE=429, VRAM=503, TOOL=400/403/502, CFG=500."""
    assert AUTH_INVALID_CREDS.http_status == 401
    assert RATE_PER_USER_MINUTE.http_status == 429
    assert RATE_PER_USER_DAY.http_status == 429
    assert VRAM_EXHAUSTED.http_status == 503
    assert TOOL_INVALID_ARGS.http_status == 400
    assert CFG_AUTH_SECRET_MISSING.http_status == 500
    assert INTERNAL_UNEXPECTED.http_status == 500


def test_raise_with_code_default_detail() -> None:
    with pytest.raises(HTTPException) as exc:
        raise_with_code(AUTH_INVALID_CREDS)
    assert exc.value.status_code == 401
    assert exc.value.detail == {
        "code": "LAI-AUTH-001",
        "detail": AUTH_INVALID_CREDS.summary,
    }
    # Header so log aggregators don't have to parse the body.
    assert exc.value.headers["X-LAI-Error-Code"] == "LAI-AUTH-001"


def test_raise_with_code_custom_detail() -> None:
    with pytest.raises(HTTPException) as exc:
        raise_with_code(VRAM_EXHAUSTED, detail="need 18.1 GB, NVML free 4.2 GB")
    assert exc.value.detail["detail"] == "need 18.1 GB, NVML free 4.2 GB"
    assert exc.value.detail["code"] == "LAI-VRAM-001"


def test_raise_with_code_custom_http_status() -> None:
    """A tool argument validation failure is documented as 400 by
    default but a caller can pick 422 if the situation warrants. The
    override is on the call, not the error code (codes are stable)."""
    with pytest.raises(HTTPException) as exc:
        raise_with_code(TOOL_INVALID_ARGS, http_status=422)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "LAI-TOOL-004"


def test_raise_with_code_includes_extra() -> None:
    with pytest.raises(HTTPException) as exc:
        raise_with_code(VRAM_EXHAUSTED, extra={"need_gb": 18.1, "free_gb": 4.2})
    assert exc.value.detail["extra"] == {"need_gb": 18.1, "free_gb": 4.2}


def test_format_error_default_summary() -> None:
    s = format_error(AUTH_INVALID_CREDS)
    assert s == f"[LAI-AUTH-001] {AUTH_INVALID_CREDS.summary}"


def test_format_error_custom_detail() -> None:
    s = format_error(VRAM_EXHAUSTED, "need 18.1 GB")
    assert s == "[LAI-VRAM-001] need 18.1 GB"
