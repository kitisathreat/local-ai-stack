"""Unit tests for backend/middleware/* — the modules ported from the
legacy Open WebUI pipelines/ directory in Phase 6."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def run(coro):
    return asyncio.run(coro)


# ── context.py ──────────────────────────────────────────────────────────

def test_context_injection_creates_system_message():
    from backend.middleware.context import inject_system_context
    from backend.schemas import ChatMessage

    messages = [ChatMessage(role="user", content="Hi")]
    out = inject_system_context(messages, inject_datetime=True)
    assert out[0].role == "system"
    assert "[Context:" in out[0].content
    assert "Current date" in out[0].content


def test_context_appends_to_existing_system():
    from backend.middleware.context import inject_system_context
    from backend.schemas import ChatMessage

    messages = [
        ChatMessage(role="system", content="You are helpful."),
        ChatMessage(role="user", content="Hi"),
    ]
    inject_system_context(messages, inject_datetime=True)
    assert messages[0].content.startswith("You are helpful.")
    assert "[Context:" in messages[0].content


def test_context_with_no_fields_is_noop():
    from backend.middleware.context import inject_system_context
    from backend.schemas import ChatMessage

    messages = [ChatMessage(role="user", content="Hi")]
    out = inject_system_context(
        messages,
        inject_datetime=False, inject_system_info=False, custom_text="",
    )
    # No system message added
    assert all(m.role != "system" for m in out)


# ── clarification.py ────────────────────────────────────────────────────

def test_clarification_instruction_injected():
    from backend.middleware.clarification import inject_clarification_instruction
    from backend.schemas import ChatMessage

    messages = [ChatMessage(role="user", content="Do something for me")]
    inject_clarification_instruction(messages)
    sys_msg = next(m for m in messages if m.role == "system")
    assert "Clarification Protocol" in sys_msg.content


def test_clarification_not_re_injected_after_recent_clarify():
    from backend.middleware.clarification import inject_clarification_instruction
    from backend.schemas import ChatMessage

    messages = [
        ChatMessage(role="user", content="do thing"),
        ChatMessage(role="assistant", content="[CLARIFY]Q: x\nO: a|b[/CLARIFY]"),
        ChatMessage(role="user", content="a"),
    ]
    before = len(messages)
    inject_clarification_instruction(messages)
    # No new system message appended
    assert len([m for m in messages if m.role == "system"]) == 0
    assert len(messages) == before


def test_format_clarifications_renders_numbered_options():
    from backend.middleware.clarification import format_clarifications

    raw = (
        "Here's my question.\n\n"
        "[CLARIFY]\nQ: What is your goal?\nO: Finish fast | Do it well | Not sure\n[/CLARIFY]"
    )
    out = format_clarifications(raw)
    assert "🤔" in out
    assert "1️⃣" in out and "2️⃣" in out and "3️⃣" in out
    assert "Finish fast" in out and "Do it well" in out


def test_format_clarifications_passes_through_non_clarify():
    from backend.middleware.clarification import format_clarifications

    assert format_clarifications("Just a normal reply.") == "Just a normal reply."


def test_is_likely_ambiguous_flags_vague():
    from backend.middleware.clarification import is_likely_ambiguous

    assert is_likely_ambiguous("can you help me with something") is True


def test_is_likely_ambiguous_ignores_clear():
    from backend.middleware.clarification import is_likely_ambiguous

    assert is_likely_ambiguous("What is the capital of France?") is False
    assert is_likely_ambiguous("How do I sort a list in Python") is False
    assert is_likely_ambiguous("Hi") is False  # too short


# ── web_search.py ───────────────────────────────────────────────────────

def test_web_search_trigger_detection():
    from backend.middleware.web_search import needs_search

    assert needs_search("What's the weather today?") is True
    assert needs_search("latest news on foo") is True
    assert needs_search("stock price of AAPL") is True
    assert needs_search("Tell me about Shakespeare") is False
    assert needs_search("") is False


def test_web_search_always_flag_forces_true():
    from backend.middleware.web_search import needs_search

    assert needs_search("anything", always=True) is True


def test_web_search_format_results_renders_lines():
    from backend.middleware.web_search import format_results

    results = [
        {"title": "X", "content": "Snippet of X", "url": "https://x.io"},
        {"title": "Y", "content": "About Y", "url": "https://y.io"},
    ]
    out = format_results(results)
    assert out.startswith("[Web Search Results:")
    assert "X: Snippet of X (https://x.io)" in out
    assert "Y: About Y (https://y.io)" in out


def test_web_search_format_empty():
    from backend.middleware.web_search import format_results
    assert format_results([]) == ""


def test_web_search_injection_triggers(monkeypatch):
    """When the user message contains a trigger keyword, results are appended."""
    from backend.middleware.web_search import inject_web_results
    from backend.schemas import ChatMessage

    mock_results = [{"title": "Latest", "content": "news blurb", "url": "https://news.io"}]

    async def fake_search(_query: str):
        return mock_results

    monkeypatch.setattr("backend.middleware.web_search.search", fake_search)

    messages = [ChatMessage(role="user", content="What is the latest news on X?")]
    run(inject_web_results(messages))
    assert "news blurb" in messages[0].content
    assert "https://news.io" in messages[0].content


def test_web_search_injection_skipped_on_neutral(monkeypatch):
    from backend.middleware.web_search import inject_web_results
    from backend.schemas import ChatMessage

    called = False

    async def fake_search(_query: str):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr("backend.middleware.web_search.search", fake_search)

    messages = [ChatMessage(role="user", content="Explain the theory of relativity")]
    run(inject_web_results(messages))
    assert called is False  # no trigger → no call


# ── rate_limit.py ───────────────────────────────────────────────────────

def test_rate_limiter_allows_below_minute_threshold():
    from backend.middleware.rate_limit import RateLimiter

    rl = RateLimiter(per_minute=5, per_day=100)
    for _ in range(5):
        rl.check("u1")
    # Within budget — no raise.


def test_rate_limiter_blocks_over_minute_threshold():
    from backend.middleware.rate_limit import RateLimiter
    from fastapi import HTTPException

    rl = RateLimiter(per_minute=3, per_day=100)
    for _ in range(3):
        rl.check("u1")
    with pytest.raises(HTTPException) as excinfo:
        rl.check("u1")
    assert excinfo.value.status_code == 429


def test_rate_limiter_isolates_per_user():
    from backend.middleware.rate_limit import RateLimiter

    rl = RateLimiter(per_minute=2, per_day=100)
    rl.check("u1"); rl.check("u1")
    # u2 has its own budget
    rl.check("u2"); rl.check("u2")


def test_rate_limiter_exempts_admin_role():
    from backend.middleware.rate_limit import RateLimiter

    rl = RateLimiter(per_minute=1, per_day=1)
    # admin not limited
    for _ in range(10):
        rl.check("admin1", role="admin")


def test_rate_limiter_blocks_on_day_threshold():
    from backend.middleware.rate_limit import RateLimiter
    from fastapi import HTTPException

    rl = RateLimiter(per_minute=1000, per_day=2)
    rl.check("u1"); rl.check("u1")
    with pytest.raises(HTTPException) as excinfo:
        rl.check("u1")
    assert excinfo.value.status_code == 429
    assert "Daily" in excinfo.value.detail
