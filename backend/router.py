"""Request router — decides tier, thinking mode, and single-agent vs
multi-agent dispatch.

Precedence for thinking mode (highest wins):
    1. Explicit `think` field on the request
    2. Slash command `/think on|off` parsed from last user message
    3. Tier-level `think_default`, OVERRIDDEN BY:
    4. Auto-thinking signals from config/router.yaml

(#3 vs #4 intentional: auto-detection is the smartest signal and should
override the tier default; explicit user intent is stronger still.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .config import AppConfig, CompiledSignals, TierConfig
from .schemas import ChatMessage, ChatRequest, MessagePart, RouteDecision


@dataclass
class SlashParseResult:
    cleaned_message: str
    applied: list[str] = field(default_factory=list)
    set_tier: str | None = None
    set_variant: str | None = None
    think_override: bool | None = None
    multi_agent_override: bool | None = None
    clear_memory: bool = False


def parse_slash_commands(
    message: str,
    slash_map: dict[str, dict[str, Any]],
) -> SlashParseResult:
    """Strip leading slash commands from the user's message, record what
    they requested. Runs in a loop so multiple commands can chain:
        `/think off /solo What is 2+2?`
    """
    result = SlashParseResult(cleaned_message=message)
    text = message.strip()

    # Sort commands by length descending so "/think on" matches before "/think"
    ordered = sorted(slash_map.items(), key=lambda kv: -len(kv[0]))

    changed = True
    while changed:
        changed = False
        for cmd, effects in ordered:
            if not text.lower().startswith(cmd.lower()):
                continue

            # "/tier " consumes the next whitespace-delimited token
            if effects.get("set_tier"):
                remaining = text[len(cmd):].strip()
                parts = remaining.split(None, 1)
                if parts:
                    result.set_tier = parts[0].strip().lower().lstrip("tier.")
                    result.applied.append(f"{cmd.strip()} {parts[0]}")
                    text = parts[1] if len(parts) > 1 else ""
                    changed = True
                    break

            # "/coder " consumes the next token as the variant name.
            # Aliases small/big -> 30b/80b for natural-language ergonomics.
            if effects.get("set_variant"):
                remaining = text[len(cmd):].strip()
                parts = remaining.split(None, 1)
                if parts:
                    raw = parts[0].strip().lower()
                    aliases = {"small": "30b", "big": "80b", "large": "80b"}
                    result.set_variant = aliases.get(raw, raw)
                    result.applied.append(f"{cmd.strip()} {parts[0]}")
                    text = parts[1] if len(parts) > 1 else ""
                    changed = True
                    break

            if "think" in effects:
                result.think_override = bool(effects["think"])
            if "multi_agent" in effects:
                result.multi_agent_override = bool(effects["multi_agent"])
            if effects.get("clear_memory"):
                result.clear_memory = True

            result.applied.append(cmd.strip())
            text = text[len(cmd):].lstrip()
            changed = True
            break

    result.cleaned_message = text
    return result


def last_user_text(messages: list[ChatMessage]) -> str:
    """Extract the trailing user message as a plain string (joins any
    multimodal text parts)."""
    for msg in reversed(messages):
        if msg.role != "user":
            continue
        if isinstance(msg.content, str):
            return msg.content
        return " ".join(p.text or "" for p in msg.content if p.type == "text")
    return ""


def has_image(messages: list[ChatMessage]) -> bool:
    for msg in messages:
        if isinstance(msg.content, list):
            if any(p.type == "image_url" for p in msg.content):
                return True
    return False


_CODE_BLOCK_RE = re.compile(r"```[\s\S]+?```")


def has_code_block(text: str) -> bool:
    return bool(_CODE_BLOCK_RE.search(text))


def auto_think_decision(
    text: str,
    signals: CompiledSignals,
) -> bool | None:
    """Returns True to force on, False to force off, None for no signal."""
    for pat in signals.disable_thinking:
        if pat.search(text):
            return False
    for pat in signals.enable_thinking:
        if pat.search(text):
            return True
    for rule in signals.think_keyword_rules:
        words = [w.lower() for w in rule.get("words", [])]
        min_count = int(rule.get("min", 1))
        hits = sum(1 for w in words if re.search(rf"\b{re.escape(w)}\b", text, re.IGNORECASE))
        if hits >= min_count:
            return True
    return None


def multi_agent_decision(
    text: str,
    signals: CompiledSignals,
) -> bool:
    for pat in signals.multi_agent_triggers:
        if pat.search(text):
            return True
    if signals.multi_agent_question_mark_min is not None:
        if text.count("?") >= signals.multi_agent_question_mark_min:
            return True
    # estimated_output_tokens_gt is a caller-supplied estimate; not applied here
    return False


def resolve_thinking(
    text: str,
    tier: TierConfig,
    explicit: bool | None,
    slash_override: bool | None,
    signals: CompiledSignals,
) -> bool:
    if not tier.think_supported:
        return False
    if explicit is not None:
        return explicit
    if slash_override is not None:
        return slash_override
    auto = auto_think_decision(text, signals)
    if auto is not None:
        return auto
    return tier.think_default


def route(
    req: ChatRequest,
    config: AppConfig,
    signals: CompiledSignals,
) -> tuple[RouteDecision, ChatRequest]:
    """Main entry — returns the decision plus a potentially-modified request
    (user message stripped of slash commands, model normalized to canonical
    tier name)."""

    slash_map = config.router.slash_commands
    last_text = last_user_text(req.messages)
    parsed = parse_slash_commands(last_text, slash_map)

    # Rewrite the last user message with slash commands stripped
    if parsed.applied:
        for i in range(len(req.messages) - 1, -1, -1):
            if req.messages[i].role == "user":
                if isinstance(req.messages[i].content, str):
                    req.messages[i].content = parsed.cleaned_message
                else:
                    # Replace text parts with cleaned version, preserving images
                    kept: list[MessagePart] = []
                    text_done = False
                    for part in req.messages[i].content:
                        if part.type == "text" and not text_done:
                            kept.append(MessagePart(type="text", text=parsed.cleaned_message))
                            text_done = True
                        elif part.type != "text":
                            kept.append(part)
                    req.messages[i].content = kept
                break

    # Resolve tier — slash override > request.model
    tier_name_input = parsed.set_tier or req.model
    tier_name, tier = config.models.resolve(tier_name_input)

    # Virtual team tier (`multi_agent`): the user picked the "Multi-Agent
    # Team" entry in the tier dropdown. That tier owns no llama-server
    # of its own — its job is to (a) force multi-agent ON and (b) pin
    # the orchestrator tier to whatever the chat panel sent. Resolve
    # `tier_name` to the orchestrator tier here, before the rest of
    # the router runs, so downstream code (specialist routing, thinking
    # signals, vision auto-route) operates on a real tier and the
    # orchestrator is correctly spawned for the synthesis step.
    team_tier_picked = (getattr(tier, "role", None) == "team")
    if team_tier_picked:
        # Force multi-agent on for this request, regardless of any
        # other heuristic, by mutating the request's options. Defaults
        # come from router.multi_agent in config; the chat UI's team
        # panel populates orchestrator_tier / worker_tier / num_workers
        # over the wire, so per-request overrides win as usual.
        ma_opts = req.multi_agent_options
        if ma_opts is None:
            from .schemas import MultiAgentOptions  # local import — schemas.py imports config indirectly
            ma_opts = MultiAgentOptions()
            req.multi_agent_options = ma_opts
        ma_opts.enabled = True
        # Pick the orchestrator tier: per-request override > config default.
        orch_pick = (ma_opts.orchestrator_tier
                     or config.router.multi_agent.orchestrator_tier)
        if orch_pick and orch_pick.startswith("tier."):
            orch_pick = orch_pick[5:]
        if orch_pick in config.models.tiers:
            tier_name, tier = config.models.resolve(orch_pick)

    # Specialist auto-routing (vision/coding) unless user picked vision explicitly
    specialist_reason: str | None = None
    if has_image(req.messages) and tier_name != "vision":
        tier_name, tier = config.models.resolve("vision")
        specialist_reason = "image_in_message"
    elif has_code_block(parsed.cleaned_message) and tier_name not in ("coding", "vision"):
        # Only auto-route to coding if there's a code block AND we're not
        # already on vision (vision can handle code screenshots)
        if "coding" in config.models.tiers:
            tier_name, tier = config.models.resolve("coding")
            specialist_reason = "code_block_present"

    # Thinking mode
    think = resolve_thinking(
        text=parsed.cleaned_message,
        tier=tier,
        explicit=req.think,
        slash_override=parsed.think_override,
        signals=signals,
    )

    # Multi-agent? Precedence (highest first):
    #   1. multi_agent_options.enabled (per-chat panel toggle)
    #   2. legacy `multi_agent` flag
    #   3. /solo, /swarm slash commands
    #   4. signal heuristics from router.yaml
    opt_enabled = (
        req.multi_agent_options.enabled
        if req.multi_agent_options is not None else None
    )
    if opt_enabled is not None:
        multi_agent = opt_enabled
    elif req.multi_agent is not None:
        multi_agent = req.multi_agent
    elif parsed.multi_agent_override is not None:
        multi_agent = parsed.multi_agent_override
    else:
        multi_agent = multi_agent_decision(parsed.cleaned_message, signals)

    # Disable multi-agent for Fast tier (overhead exceeds benefit) and Vision
    # (image routing is already a specialist path).
    if tier_name in ("fast", "vision"):
        multi_agent = False

    # Variant override: only honored when the resolved tier declares the
    # named variant. Silently dropped otherwise so a stray /coder on a
    # non-coding tier doesn't error. Resolution order:
    #   1. explicit ChatRequest.variant field (UI's variant sub-selector)
    #   2. /coder big | small slash command in the user message
    variant: str | None = None
    if req.variant and req.variant in tier.variants:
        variant = req.variant
    elif parsed.set_variant and parsed.set_variant in tier.variants:
        variant = parsed.set_variant

    decision = RouteDecision(
        tier_name=tier_name,
        think=think,
        multi_agent=multi_agent,
        variant=variant,
        slash_commands_applied=parsed.applied,
        overrides={
            k: v for k, v in {
                "explicit_think": req.think,
                "slash_think": parsed.think_override,
                "slash_multi_agent": parsed.multi_agent_override,
                "clear_memory": parsed.clear_memory or None,
                "slash_variant": parsed.set_variant,
            }.items() if v is not None
        },
        specialist_reason=specialist_reason,
    )

    # Normalize request.model so downstream uses the canonical tier name
    req.model = tier_name
    return decision, req
