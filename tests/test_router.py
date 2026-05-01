"""Unit tests for backend/router.py — tier resolution, slash-command
parsing, thinking mode precedence, specialist auto-routing."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import AppConfig, CompiledSignals
from backend.router import (
    auto_think_decision,
    has_code_block,
    has_image,
    last_user_text,
    multi_agent_decision,
    parse_slash_commands,
    resolve_thinking,
    route,
)
from backend.schemas import ChatMessage, ChatRequest, MessagePart


@pytest.fixture(scope="module")
def cfg() -> AppConfig:
    return AppConfig.load(config_dir=ROOT / "config")


@pytest.fixture(scope="module")
def signals(cfg: AppConfig) -> CompiledSignals:
    return cfg.compile_signals()


# ── Slash commands ──────────────────────────────────────────────────────

def test_slash_think_on(cfg):
    r = parse_slash_commands("/think on What is 2+2?", cfg.router.slash_commands)
    assert r.think_override is True
    assert r.cleaned_message == "What is 2+2?"
    assert "/think on" in r.applied


def test_slash_think_off(cfg):
    r = parse_slash_commands("/think off hi", cfg.router.slash_commands)
    assert r.think_override is False
    assert r.cleaned_message == "hi"


def test_slash_solo(cfg):
    r = parse_slash_commands("/solo compare A B C", cfg.router.slash_commands)
    assert r.multi_agent_override is False
    assert r.cleaned_message == "compare A B C"


def test_slash_tier_override(cfg):
    r = parse_slash_commands("/tier coding write a sort function", cfg.router.slash_commands)
    assert r.set_tier == "coding"
    assert r.cleaned_message == "write a sort function"


def test_slash_chained(cfg):
    r = parse_slash_commands("/think off /solo hello", cfg.router.slash_commands)
    assert r.think_override is False
    assert r.multi_agent_override is False
    assert r.cleaned_message == "hello"


def test_slash_no_match_passes_through(cfg):
    r = parse_slash_commands("just a normal message", cfg.router.slash_commands)
    assert r.applied == []
    assert r.cleaned_message == "just a normal message"


# ── Auto-thinking ──────────────────────────────────────────────────────

def test_auto_think_on_math_keyword(signals):
    assert auto_think_decision("Prove that sqrt(2) is irrational", signals) is True


def test_auto_think_on_step_by_step(signals):
    assert auto_think_decision("Walk me step-by-step through this", signals) is True


def test_auto_think_off_greeting(signals):
    assert auto_think_decision("Hello there", signals) is False


def test_auto_think_off_tldr(signals):
    assert auto_think_decision("Summarize in one sentence: ...", signals) is False


def test_auto_think_neutral_returns_none(signals):
    assert auto_think_decision("I'd like to know more", signals) is None


# ── Multi-agent detection ──────────────────────────────────────────────

def test_multi_agent_compare_trigger(signals):
    assert multi_agent_decision("Compare Python vs Rust", signals) is True


def test_multi_agent_research_each(signals):
    assert multi_agent_decision("Research each of these options", signals) is True


def test_multi_agent_question_marks(signals):
    assert multi_agent_decision("A? B? C? D?", signals) is True


def test_multi_agent_negative(signals):
    assert multi_agent_decision("Hi, how are you?", signals) is False


# ── Thinking precedence ────────────────────────────────────────────────

def test_thinking_explicit_overrides_auto(cfg, signals):
    tier = cfg.models.tiers["versatile"]
    # Auto would say True (math), but explicit False wins
    assert resolve_thinking(
        text="Prove that sqrt(2) is irrational",
        tier=tier,
        explicit=False,
        slash_override=None,
        signals=signals,
    ) is False


def test_thinking_slash_overrides_auto(cfg, signals):
    tier = cfg.models.tiers["versatile"]
    assert resolve_thinking(
        text="Prove that sqrt(2) is irrational",
        tier=tier,
        explicit=None,
        slash_override=False,
        signals=signals,
    ) is False


def test_thinking_auto_overrides_default(cfg, signals):
    tier = cfg.models.tiers["versatile"]  # default think=False
    # No explicit/slash, but auto triggers on "prove"
    assert resolve_thinking(
        text="Prove the Pythagorean theorem",
        tier=tier,
        explicit=None,
        slash_override=None,
        signals=signals,
    ) is True


def test_thinking_default_when_no_signal(cfg, signals):
    # All Qwen3 tiers now ship with think_default=False — Qwen3 emits long
    # reasoning_content blocks the chat UI silently drops, making responses
    # appear stuck. Users opt in via the Think checkbox / /think on.
    # When there's no explicit override, no slash override, and no auto
    # signal, resolve_thinking should fall through to tier.think_default.
    #
    # NOTE: highest_quality is now Qwen3-Next-80B-A3B-Thinking, an
    # explicitly thinking-only variant — so its think_default is True.
    # The "fall-through to tier default" semantics are tested here against
    # versatile, which still defaults to False.
    tier = cfg.models.tiers["versatile"]
    assert tier.think_default is False
    assert resolve_thinking(
        text="random neutral message",
        tier=tier,
        explicit=None,
        slash_override=None,
        signals=signals,
    ) is False


def test_thinking_default_true_for_thinking_variant(cfg, signals):
    """The highest_quality tier hosts the Qwen3-Next-Thinking variant
    whose chat template auto-injects <think>. think_default=True ensures
    the router doesn't accidentally suppress reasoning on that tier."""
    tier = cfg.models.tiers["highest_quality"]
    assert tier.think_default is True
    assert resolve_thinking(
        text="random neutral message",
        tier=tier,
        explicit=None,
        slash_override=None,
        signals=signals,
    ) is True


# ── Image & code detection ─────────────────────────────────────────────

def test_has_image_true():
    msg = ChatMessage(role="user", content=[
        MessagePart(type="text", text="Describe"),
        MessagePart(type="image_url", image_url={"url": "data:image/png;base64,..."}),
    ])
    assert has_image([msg]) is True


def test_has_image_false():
    msg = ChatMessage(role="user", content="no image here")
    assert has_image([msg]) is False


def test_has_code_block_true():
    assert has_code_block("```python\nprint('hi')\n```") is True


def test_has_code_block_false():
    assert has_code_block("just prose, no backticks") is False


# ── Full route pipeline ────────────────────────────────────────────────

def test_route_defaults_to_versatile(cfg, signals):
    req = ChatRequest(
        model=cfg.models.default,
        messages=[ChatMessage(role="user", content="Hello")],
    )
    decision, _ = route(req, cfg, signals)
    assert decision.tier_name == "versatile"
    assert decision.multi_agent is False


def test_route_image_forces_vision(cfg, signals):
    req = ChatRequest(
        model="versatile",
        messages=[ChatMessage(role="user", content=[
            MessagePart(type="text", text="What's in this?"),
            MessagePart(type="image_url", image_url={"url": "data:image/png;base64,xyz"}),
        ])],
    )
    decision, _ = route(req, cfg, signals)
    assert decision.tier_name == "vision"
    assert decision.specialist_reason == "image_in_message"


def test_route_code_block_forces_coding(cfg, signals):
    req = ChatRequest(
        model="versatile",
        messages=[ChatMessage(role="user", content="Fix this:\n```py\nprint(x)\n```")],
    )
    decision, _ = route(req, cfg, signals)
    assert decision.tier_name == "coding"
    assert decision.specialist_reason == "code_block_present"


def test_route_slash_tier_wins(cfg, signals):
    req = ChatRequest(
        model="versatile",
        messages=[ChatMessage(role="user", content="/tier fast just chat")],
    )
    decision, req2 = route(req, cfg, signals)
    assert decision.tier_name == "fast"


def test_route_alias_resolves(cfg, signals):
    req = ChatRequest(
        model="quality",  # old alias
        messages=[ChatMessage(role="user", content="hi")],
    )
    decision, _ = route(req, cfg, signals)
    assert decision.tier_name == "highest_quality"


def test_route_multi_agent_trigger(cfg, signals):
    req = ChatRequest(
        model="versatile",
        messages=[ChatMessage(role="user", content="Compare Python, Rust, and Go")],
    )
    decision, _ = route(req, cfg, signals)
    assert decision.multi_agent is True


def test_route_multi_agent_disabled_for_fast(cfg, signals):
    req = ChatRequest(
        model="fast",
        messages=[ChatMessage(role="user", content="Compare A, B, C")],
    )
    decision, _ = route(req, cfg, signals)
    # Multi-agent is off for Fast tier (overhead exceeds benefit)
    assert decision.multi_agent is False


def test_route_slash_solo_disables_multi_agent(cfg, signals):
    req = ChatRequest(
        model="versatile",
        messages=[ChatMessage(role="user", content="/solo Compare A, B, C")],
    )
    decision, _ = route(req, cfg, signals)
    assert decision.multi_agent is False


def test_route_strips_slash_from_message(cfg, signals):
    req = ChatRequest(
        model="versatile",
        messages=[ChatMessage(role="user", content="/think on Hello")],
    )
    decision, req2 = route(req, cfg, signals)
    assert req2.messages[-1].content == "Hello"
    assert decision.think is True
