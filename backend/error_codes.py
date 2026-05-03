"""Stable internal error codes for backend errors that bubble up to users.

The motivation: surface a short, machine-parseable identifier alongside
every user-facing error message so:

  - users / operators can grep logs and the chat UI can branch on a
    code without scraping prose,
  - codes are stable across rewrites of the human-readable text (we can
    rephrase "couldn't load model" without breaking client logic that
    keys off ``LAI-VRAM-001``),
  - third-party integrators (Open WebUI, scripts) get a contract.

Format: ``LAI-<SUBSYSTEM>-<3-DIGIT-NNN>``. SUBSYSTEM is fixed per area;
the numeric suffix is allocated incrementally and never re-used (gaps
are fine — codes are retired, not reassigned).

Standard HTTP-RFC mappings:

  - ``LAI-AUTH-*`` correspond to the 401 / 403 / 503 family
  - ``LAI-VRAM-*`` correspond to 503 (resource exhausted) — there's no
    single RFC code for "GPU memory exhausted", so 503 + an LAI- code
    is the convention here
  - ``LAI-RATE-*`` correspond to 429
  - ``LAI-MODEL-*`` correspond to 502 / 503 (upstream / unavailable)
  - ``LAI-TOOL-*`` correspond to 400 / 502 depending on cause
  - ``LAI-CFG-*`` correspond to 500 (server misconfiguration)

The chat UI distinguishes 401 / 429 / 5xx and surfaces the code +
``detail`` from the response body so the user gets actionable text
("server misconfigured: contact admin" vs "wrong password").

When raising HTTPException, prefer ``raise_with_code(...)`` so the
``X-LAI-Error-Code`` response header is set in addition to the JSON
``code`` field — same information, accessible without parsing the
body (useful for cloudflared logs and ops dashboards).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException


# ── Subsystem prefixes ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ErrorCode:
    """One stable identifier + its default HTTP status + a one-liner.

    The HTTP status is a default; callers can override at raise-time when
    a more specific status fits (e.g. a tool-validation error that's
    really a 400 even though the default is 502).
    """

    code: str
    http_status: int
    summary: str


# Auth / session
AUTH_INVALID_CREDS         = ErrorCode("LAI-AUTH-001", 401, "Invalid username or password.")
AUTH_NOT_AUTHENTICATED     = ErrorCode("LAI-AUTH-002", 401, "Not authenticated.")
AUTH_SESSION_EXPIRED       = ErrorCode("LAI-AUTH-003", 401, "Session expired.")
AUTH_NOT_ADMIN             = ErrorCode("LAI-AUTH-004", 403, "Admin account required.")
AUTH_AIRGAP_BLOCKED        = ErrorCode("LAI-AUTH-005", 403, "Airgap mode blocks remote access.")
AUTH_HOST_NOT_ALLOWED      = ErrorCode("LAI-AUTH-006", 403, "Host not allowed.")
AUTH_CANNOT_SIGN_TOKEN     = ErrorCode("LAI-AUTH-007", 503, "Server cannot sign session tokens.")

# Rate limiting
RATE_PER_USER_MINUTE       = ErrorCode("LAI-RATE-001", 429, "Too many requests this minute.")
RATE_PER_USER_DAY          = ErrorCode("LAI-RATE-002", 429, "Daily request limit reached.")
RATE_LOGIN_PER_IP_HOUR     = ErrorCode("LAI-RATE-003", 429, "Too many sign-in attempts.")

# VRAM scheduler
VRAM_EXHAUSTED             = ErrorCode("LAI-VRAM-001", 503, "VRAM exhausted — tier doesn't fit.")
VRAM_QUEUE_FULL            = ErrorCode("LAI-VRAM-002", 503, "Per-tier wait queue is full.")
VRAM_QUEUE_TIMEOUT         = ErrorCode("LAI-VRAM-003", 503, "Wait queue timed out before a slot opened.")
VRAM_NVML_MISMATCH         = ErrorCode("LAI-VRAM-004", 503, "NVML free-VRAM reading disagrees with scheduler projection.")
VRAM_LOAD_FAILED           = ErrorCode("LAI-VRAM-005", 502, "llama-server failed to load the GGUF.")

# Models / config
MODEL_GGUF_MISSING         = ErrorCode("LAI-MODEL-001", 503, "Tier's GGUF is not on disk yet.")
MODEL_TIER_UNKNOWN         = ErrorCode("LAI-MODEL-002", 400, "Unknown tier.")
MODEL_VARIANT_UNKNOWN      = ErrorCode("LAI-MODEL-003", 400, "Unknown tier variant.")
MODEL_BACKEND_DOWN         = ErrorCode("LAI-MODEL-004", 502, "llama-server backend unreachable.")
MODEL_PULL_FAILED          = ErrorCode("LAI-MODEL-005", 502, "Hugging Face pull failed.")

# Tools
TOOL_NOT_FOUND             = ErrorCode("LAI-TOOL-001", 404, "Tool not found in registry.")
TOOL_DISABLED              = ErrorCode("LAI-TOOL-002", 403, "Tool is disabled by admin.")
TOOL_AIRGAP_BLOCKED        = ErrorCode("LAI-TOOL-003", 403, "Tool requires network access (blocked in airgap mode).")
TOOL_INVALID_ARGS          = ErrorCode("LAI-TOOL-004", 400, "Tool arguments failed validation.")
TOOL_UPSTREAM_FAILED       = ErrorCode("LAI-TOOL-005", 502, "Tool's upstream service returned an error.")

# RAG / memory
RAG_QDRANT_DOWN            = ErrorCode("LAI-RAG-001", 502, "Qdrant vector store unreachable.")
RAG_EMBEDDING_DOWN         = ErrorCode("LAI-RAG-002", 502, "Embedding tier unreachable.")
RAG_DOC_TOO_LARGE          = ErrorCode("LAI-RAG-003", 413, "Document exceeds upload size limit.")
RAG_DIMENSION_MISMATCH     = ErrorCode("LAI-RAG-004", 500, "Embedding dimension mismatch — re-embed required.")

# Configuration / startup
CFG_AUTH_SECRET_MISSING    = ErrorCode("LAI-CFG-001", 500, "AUTH_SECRET_KEY env var is not set.")
CFG_HISTORY_KEY_MISSING    = ErrorCode("LAI-CFG-002", 500, "HISTORY_SECRET_KEY env var is not set.")
CFG_INVALID_YAML           = ErrorCode("LAI-CFG-003", 500, "Configuration YAML failed validation.")
CFG_TOOLS_REGISTRY_EMPTY   = ErrorCode("LAI-CFG-004", 500, "Tool registry loaded zero tools.")

# Chat / conversation surface
CHAT_NOT_FOUND             = ErrorCode("LAI-CHAT-001", 404, "Conversation not found.")
CHAT_AIRGAP_MISMATCH       = ErrorCode("LAI-CHAT-002", 404, "Conversation belongs to a different airgap mode.")
CHAT_ATTACHMENT_TOO_LARGE  = ErrorCode("LAI-CHAT-003", 413, "Attachment exceeds size limit.")

# Generic catch-all (avoid using — pick a specific code instead)
INTERNAL_UNEXPECTED        = ErrorCode("LAI-INT-999", 500, "Unexpected server error.")


# ── Helpers ─────────────────────────────────────────────────────────────────


def raise_with_code(
    code: ErrorCode,
    detail: str | None = None,
    *,
    http_status: int | None = None,
    extra: dict[str, Any] | None = None,
) -> "Any":   # type: ignore[no-untyped-def]
    """Raise an HTTPException whose JSON body and headers carry the code.

    The body shape is::

        {
          "code": "LAI-VRAM-001",
          "detail": "VRAM exhausted: need 18.1 GB, NVML free 4.2 GB, ...",
          "extra": {...}            # optional per-error structured data
        }

    The ``X-LAI-Error-Code`` response header carries the code too so log
    aggregators / cloudflared / ops dashboards can match without parsing
    the body. ``detail`` defaults to the code's standard summary; pass
    ``detail`` to provide situation-specific text (e.g. live VRAM
    numbers in a VRAM exhaustion error).
    """
    payload: dict[str, Any] = {
        "code": code.code,
        "detail": detail or code.summary,
    }
    if extra:
        payload["extra"] = extra
    raise HTTPException(
        status_code=http_status or code.http_status,
        detail=payload,
        headers={"X-LAI-Error-Code": code.code},
    )


def format_error(code: ErrorCode, detail: str | None = None) -> str:
    """Format a code + detail line for log output. Use in places that
    raise non-HTTP exceptions (e.g. VRAMExhausted) so the same
    machine-readable identifier reaches the chat UI's error event."""
    return f"[{code.code}] {detail or code.summary}"
