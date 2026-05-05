"""Eval runner. Talks to the live backend's tier endpoint, scores each
problem, aggregates pass-rate + per-question latency, writes JSON.

Capability → dataset mapping:
    reasoning   → AIME 2024
    math        → GSM8K
    coding      → HumanEval
    knowledge   → MMLU subset
    long_context → needle-in-haystack

Designed so that `--depth fast --capabilities all` across a single tier
runs in ~30 minutes on a mid-tier (~25 tok/s). Overnight `--depth full`
across all tiers is the definitive bench.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from .datasets import (
    Depth,
    Problem,
    load_aime2024,
    load_gsm8k,
    load_humaneval,
    load_ifeval,
    load_math,
    load_mbpp,
    load_mmlu,
    load_mmlu_pro,
    load_mtbench,
    load_needle,
)
from . import graders as _graders_mod
from .graders import score


logger = logging.getLogger("backend.eval")


# Capability → loader. Order matters: reasoning + coding first so the slow
# tiers' painful prompts hit the wall-clock deadline first if we hit it.
CAPABILITIES: dict[str, Callable[[Depth], list[Problem]]] = {
    "reasoning": load_aime2024,
    "coding": load_humaneval,
    "coding_basic": load_mbpp,                 # complement to humaneval
    "math": load_gsm8k,
    "math_competition": load_math,             # MATH-500 levels 3-5
    "knowledge": load_mmlu,
    "knowledge_specialized": load_mmlu_pro,    # MMLU-Pro 10-choice
    "intent": load_ifeval,                     # instruction-following
    "clarity": load_mtbench,                   # LLM-as-judge MT-Bench
    "long_context": load_needle,
}


@dataclass
class ProblemResult:
    id: str
    kind: str
    passed: bool
    latency_s: float
    output_tokens: int
    output_text_len: int
    error: str | None = None
    # truncated to 200 chars in the JSON to keep file size bounded;
    # full text is in the per-tier log file.
    output_preview: str = ""


@dataclass
class TierResult:
    tier: str
    capability: str
    depth: Depth
    started_at: float
    finished_at: float
    n_problems: int
    n_passed: int
    pass_rate: float
    mean_latency_s: float
    p95_latency_s: float
    # Condition tags so the persisted JSON is self-describing for resume
    # mode (without these, the bench loader has to guess which think/tools
    # combo each cell was for).
    think: str | None = None
    tools: str | None = None
    problems: list[ProblemResult] = field(default_factory=list)
    # Problems excluded because the prompt exceeds the tier's context
    # window (e.g. needle-ctx65536 on swarm with ctx_window=16k). These
    # are NOT counted in n_problems / pass_rate — running them would
    # produce empty responses that are methodology artifacts, not signal.
    n_skipped_ctx: int = 0
    skipped_ids: list[str] = field(default_factory=list)
    # Set when the cell aborted before exhausting its problem list. The
    # bench script reads this to decide whether to re-warm the tier
    # (it does for "consecutive_zero_tok" since that means the
    # llama-server has crashed mid-cell).
    abort_reason: str | None = None

    @property
    def wall_seconds(self) -> float:
        return self.finished_at - self.started_at


# ── Backend chat (streaming SSE → flat string) ────────────────────────────

def _chat(
    api_base: str,
    tier_id: str,
    prompt: str,
    *,
    max_tokens: int,
    think: bool,
    timeout: int,
    force_web_search: bool = False,
    disable_web_search: bool = True,
    sampling_overlay: dict | None = None,
    multi_agent: bool = False,
    multi_agent_options: dict | None = None,
) -> tuple[str, int, float]:
    """One chat completion. Returns (text, output_token_estimate, wall_s).

    We use streaming so a hung tier shows up as a slow per-line read
    rather than a single 600 s timeout. Token count is whitespace-split
    on the response — same proxy used by `bench_tiers.py`.

    `force_web_search=True` makes the backend's web-search middleware
    inject results regardless of the trigger heuristic. `disable_web_search`
    skips the middleware entirely (the bench's default — we want clean
    no-tools cells). When both are False the router's heuristic decides."""
    body = {
        "model": tier_id,
        "stream": True,
        "think": think,
        # Force the requested tier — never let the auto-router promote
        # a single problem to multi-agent (which loads the orchestrator
        # tier and evicts the tier we're benching, mid-cell). Observed
        # May 2026: one MMLU problem matched the multi-agent heuristic
        # and triggered a versatile-load cascade that wiped coding from
        # GPU and failed every subsequent request with tok=0.
        # Override only when the bench EXPLICITLY requests multi-agent.
        "multi_agent": bool(multi_agent),
        "force_web_search": bool(force_web_search),
        "disable_web_search": bool(disable_web_search and not force_web_search),
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if multi_agent and multi_agent_options:
        body["multi_agent_options"] = multi_agent_options
    # Sampling overlay — temperature / top_p / top_k / min_p /
    # repeat_penalty / freq_penalty / presence_penalty. Forwarded
    # verbatim. Only set keys that are present in the overlay; missing
    # keys fall through to the tier's YAML defaults inside the backend.
    if sampling_overlay:
        for k in ("temperature", "top_p", "top_k", "min_p",
                  "repeat_penalty", "frequency_penalty", "presence_penalty"):
            v = sampling_overlay.get(k)
            if v is not None:
                body[k] = v
    req = urllib.request.Request(
        api_base.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", "accept": "text/event-stream"},
    )
    started = time.time()
    chunks: list[str] = []
    reasoning_chunks: list[str] = []
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            for choice in obj.get("choices", []):
                delta = choice.get("delta", {})
                content = delta.get("content")
                rc = delta.get("reasoning_content")
                if content:
                    chunks.append(content)
                if rc:
                    reasoning_chunks.append(rc)
    # Concatenate reasoning before final content so the grader's
    # last-occurrence regex picks up the answer marker (which lives in
    # `content`, after the model emits </think>). Without this the
    # grader sees only `content`, which is empty for thinking-tuned
    # models that exhaust their token budget mid-thinking.
    text = "".join(reasoning_chunks) + "".join(chunks)
    return text, len(text.split()), time.time() - started


# ── Main entry: run one (tier, capability) cell ──────────────────────────

def _is_response_adequate(problem: Problem, response: str) -> bool:
    """Heuristic adequacy check used by ``tools='auto'`` to decide whether
    a tool-less response was good enough or whether to retry with web
    search injected. Conservative — when in doubt, retry with tools.

    A response is considered inadequate when:
      - Empty / very short.
      - Contains explicit uncertainty markers ("I don't know", refusals).
      - For multiple-choice tasks (mmlu, mmlu_pro), no clear A-J letter.
      - For numeric tasks (gsm8k, aime2024, math), no digit at all.
      - For code tasks (humaneval, mbpp), no fenced ```python block AND
        no ``def`` keyword.
    """
    if not response or len(response.strip()) < 20:
        return False
    low = response.lower()
    UNCERTAIN = (
        "i don't know", "i do not know", "i'm not sure", "i am not sure",
        "i cannot determine", "i can't determine", "i don't have enough",
        "i'm unable to", "i'm not certain", "without more context",
        "as an ai", "i don't have access",
    )
    if any(p in low for p in UNCERTAIN):
        return False
    kind = problem.kind
    if kind in ("mmlu", "mmlu_pro"):
        return bool(re.search(r"\b[A-J]\b", response))
    if kind in ("gsm8k", "aime2024", "math", "needle"):
        return bool(re.search(r"\d", response))
    if kind in ("humaneval", "mbpp"):
        return ("```" in response) or (re.search(r"\bdef\s+\w+\(", response) is not None)
    if kind == "ifeval":
        # Any non-trivial response is plausible; let the constraint
        # grader decide. Inadequate only on empty.
        return len(response.strip()) >= 30
    if kind == "mtbench":
        # Long-form prose; ~50 chars minimum signals an attempt.
        return len(response.strip()) >= 50
    return True


def run_cell(
    api_base: str,
    tier: str,
    capability: str,
    depth: Depth,
    *,
    max_tokens: int = 2048,
    per_problem_timeout: int = 300,
    think: bool | None = None,
    deadline: float | None = None,
    tools: str = "off",
    early_stop_margin: float | None = None,
    early_stop_confidence: float = 0.95,
    early_stop_min_n: int = 30,
    use_tuned_params: bool = False,
    sampling_overlay: dict | None = None,
    tier_context_window: int | None = None,
    multi_agent: bool = False,
    multi_agent_options: dict | None = None,
) -> TierResult:
    """Run all problems in `capability` against `tier`, return aggregated
    results. `deadline` (unix ts) lets the caller cap wall time —
    problems past the deadline are skipped + recorded as 'deadline'.

    `think` defaults to True for reasoning + math (chain-of-thought helps),
    False for the rest (knowledge / coding / long_context — recall-style).

    `tools` selects how web-search is invoked per problem:
      - ``"off"`` (default): web-search middleware is skipped entirely.
        Pure model-knowledge bench.
      - ``"force"``: web-search is injected for every problem.
        Measures the upper bound of search-augmented accuracy.
      - ``"auto"``: two-pass — try without tools, check the response with
        ``_is_response_adequate``, retry with tools only if the heuristic
        says the model didn't know. Mirrors the production behaviour we
        actually want to ship: tools fire only when needed.

    Dynamic-N early-stop (``early_stop_margin`` set):
      After every problem past ``early_stop_min_n``, compute the Wilson-
      score 95%-CI (or the configured ``early_stop_confidence``) for the
      current pass-rate. If the half-width is ≤ ``early_stop_margin``,
      stop the cell early — we already have the precision we asked for.
      This shrinks N when results are decisive (p near 0 or 1) and
      forces the full dataset when results are noisy near 50/50.
    """
    import math
    import re as _re_mod  # local alias so we can hot-reload graders if needed
    # Resolve effective sampling overlay: explicit overlay wins, else
    # tuned-params lookup if enabled, else None (tier YAML defaults).
    overlay: dict | None = None
    if sampling_overlay:
        overlay = dict(sampling_overlay)
    elif use_tuned_params:
        try:
            from . import param_tuning as _pt
            persisted = _pt.get_overlay(tier, capability, think=bool(think))
            if persisted:
                overlay = persisted
        except Exception as exc:
            logger.warning("tuned-params lookup failed for %s/%s: %s", tier, capability, exc)
    loader = CAPABILITIES[capability]
    problems = loader(depth)
    # Context-window filter: drop problems whose prompt would exceed the
    # tier's configured context_window. Prevents the long-context cell
    # from getting tok=0 cascades on swarm (16k window) or HQ (32k) when
    # the dataset includes 65k targets — those failures are methodology
    # artifacts, not real model behaviour. Skipped problems are tracked
    # separately and excluded from pass-rate denominator.
    skipped_problems: list = []
    if tier_context_window is not None and tier_context_window > 0:
        keep: list = []
        # Reserve headroom for prompt template tokens + response budget
        # (the dataset uses 200 tokens itself; we add another small buffer
        # for chat-template overhead).
        ctx_budget = tier_context_window - 256
        for p in problems:
            ctx_target = (p.meta or {}).get("ctx_target") if hasattr(p, "meta") else None
            if ctx_target is not None and ctx_target > ctx_budget:
                skipped_problems.append(p)
                logger.info(
                    "  [ctx-skip] %s (ctx_target=%d > tier_window=%d)",
                    p.id, ctx_target, tier_context_window,
                )
            else:
                keep.append(p)
        if skipped_problems:
            logger.warning(
                "Cell %s/%s: skipping %d/%d problems exceeding tier ctx window %d",
                tier, capability, len(skipped_problems), len(problems),
                tier_context_window,
            )
            problems = keep
    if think is None:
        # Capabilities where chain-of-thought consistently helps:
        # competition math (AIME / MATH), MBPP-style coding, and
        # multi-step reasoning. For knowledge recall (MMLU/MMLU-Pro),
        # instruction-following (IFEval), and long-context retrieval,
        # thinking adds noise without improving accuracy.
        think = capability in {
            "reasoning", "math", "math_competition",
            "coding", "coding_basic",
        }

    logger.info(
        "eval-cell start tier=%s capability=%s depth=%s n=%d think=%s tools=%s overlay=%s",
        tier, capability, depth, len(problems), think, tools,
        overlay if overlay else "default",
    )
    started = time.time()
    results: list[ProblemResult] = []
    auto_retries = 0
    consecutive_zero_tok = 0
    ZERO_TOK_ABORT_THRESHOLD = 5
    _ABORTED_FLAG: str | None = None
    for i, problem in enumerate(problems):
        if deadline is not None and time.time() > deadline:
            logger.warning(
                "eval-cell tier=%s capability=%s deadline hit at problem %d/%d",
                tier, capability, i, len(problems),
            )
            results.append(ProblemResult(
                id=problem.id, kind=problem.kind, passed=False,
                latency_s=0.0, output_tokens=0, output_text_len=0,
                error="deadline",
            ))
            continue
        try:
            force = tools == "force"
            disable = tools == "off"
            text, n_tok, wall = _chat(
                api_base, f"tier.{tier}", problem.prompt,
                max_tokens=max_tokens, think=think,
                timeout=per_problem_timeout,
                force_web_search=force,
                disable_web_search=disable,
                sampling_overlay=overlay,
                multi_agent=multi_agent,
                multi_agent_options=multi_agent_options,
            )
            retried = False
            if tools == "auto" and not _is_response_adequate(problem, text):
                # Pass 1 looked inadequate — retry with web-search injection.
                # We keep the second response if it's non-empty regardless
                # of further adequacy heuristics (it's the model's best
                # available answer). Latency and token count include both
                # passes for honest accounting.
                text2, n_tok2, wall2 = _chat(
                    api_base, f"tier.{tier}", problem.prompt,
                    max_tokens=max_tokens, think=think,
                    timeout=per_problem_timeout,
                    force_web_search=True,
                    disable_web_search=False,
                    sampling_overlay=overlay,
                )
                if text2 and text2.strip():
                    text = text2
                wall += wall2
                n_tok += n_tok2
                retried = True
                auto_retries += 1
            passed = score(problem, text)
            preview = text[:200]
            if retried:
                preview = f"[auto-retry+tools] {preview}"
            results.append(ProblemResult(
                id=problem.id, kind=problem.kind, passed=passed,
                latency_s=wall, output_tokens=n_tok,
                output_text_len=len(text), output_preview=preview,
            ))
            logger.info(
                "  %s/%d %s tok=%d in %.1fs %s%s",
                f"{i+1:03d}", len(problems), problem.id, n_tok, wall,
                "PASS" if passed else "fail",
                " (auto-retry)" if retried else "",
            )
            # Tier-server-died detector: if N consecutive problems return
            # zero tokens, the underlying llama-server has almost certainly
            # crashed. The scheduler still thinks the tier is RESIDENT, so
            # every subsequent request will silently fail too. Abort the
            # cell early so the parent loop can move on; the next tier
            # transition (or the scheduler's own liveness check) will
            # respawn the dead server.
            if n_tok == 0:
                consecutive_zero_tok += 1
                if consecutive_zero_tok >= ZERO_TOK_ABORT_THRESHOLD:
                    logger.error(
                        "eval-cell tier=%s capability=%s aborted after %d "
                        "consecutive tok=0 responses at problem %d/%d — "
                        "tier llama-server appears dead, returning partial cell",
                        tier, capability, consecutive_zero_tok,
                        i + 1, len(problems),
                    )
                    _ABORTED_FLAG = "consecutive_zero_tok"
                    break
            else:
                consecutive_zero_tok = 0
            # Dynamic-N early-stop check. Computes a Wilson-score CI for
            # the running pass-rate; stops the cell once the half-width
            # is ≤ early_stop_margin. p̂ near 0/1 (low variance) hits
            # this fast; p̂ near 0.5 (worst-case variance) keeps going
            # until we've burned through the available problems.
            if early_stop_margin is not None and len(results) >= early_stop_min_n:
                n_real = sum(1 for r in results if r.error != "deadline")
                p_hat = sum(1 for r in results if r.passed) / max(1, n_real)
                z_table = {0.90: 1.645, 0.95: 1.96, 0.99: 2.576}
                z = z_table.get(round(early_stop_confidence, 2), 1.96)
                # Wilson 95% half-width
                z2 = z * z
                denom = 1 + z2 / n_real
                center = (p_hat + z2 / (2 * n_real)) / denom
                margin = (z * math.sqrt(p_hat * (1 - p_hat) / n_real
                                        + z2 / (4 * n_real * n_real))) / denom
                if margin <= early_stop_margin:
                    logger.info(
                        "  early-stop: n=%d p̂=%.3f Wilson half-width=%.3f ≤ %.3f",
                        n_real, p_hat, margin, early_stop_margin,
                    )
                    break
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            results.append(ProblemResult(
                id=problem.id, kind=problem.kind, passed=False,
                latency_s=0.0, output_tokens=0, output_text_len=0,
                error=f"{type(exc).__name__}: {exc}",
            ))
            logger.warning("  %s/%d %s ERROR %s", f"{i+1:03d}", len(problems),
                           problem.id, exc)

    finished = time.time()
    n_passed = sum(1 for r in results if r.passed)
    latencies = [r.latency_s for r in results if r.latency_s > 0]
    mean_lat = sum(latencies) / len(latencies) if latencies else 0.0
    if latencies:
        srt = sorted(latencies)
        p95_lat = srt[max(0, int(len(srt) * 0.95) - 1)]
    else:
        p95_lat = 0.0
    return TierResult(
        tier=tier,
        capability=capability,
        depth=depth,
        started_at=started,
        finished_at=finished,
        n_problems=len(results),
        n_passed=n_passed,
        pass_rate=n_passed / max(1, len(results)),
        mean_latency_s=mean_lat,
        p95_latency_s=p95_lat,
        problems=results,
        n_skipped_ctx=len(skipped_problems),
        skipped_ids=[p.id for p in skipped_problems],
        abort_reason=_ABORTED_FLAG,
        think="on" if think else "off",
        tools=tools,
    )


def write_json(results: Iterable[TierResult], out_path: Path) -> None:
    """Serialize the per-cell results to a single JSON for downstream
    diffing/charting. Per-problem `output_preview` is included; the full
    text lives in the eval log file."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "written_at": datetime.now().isoformat(timespec="seconds"),
        "results": [asdict(r) for r in results],
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_markdown(results: Iterable[TierResult], out_path: Path) -> None:
    """Markdown summary table — one row per (tier, capability) cell, plus
    a per-tier rollup. Designed to be drop-in-able into README sections."""
    rows = list(results)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# Capability eval results")
    lines.append("")
    lines.append(f"_Run finished {datetime.now():%Y-%m-%d %H:%M}_")
    lines.append("")
    lines.append("| tier | capability | depth | n | pass | rate | mean lat | p95 lat | wall |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        lines.append(
            f"| `{r.tier}` | {r.capability} | {r.depth} | {r.n_problems} | "
            f"{r.n_passed} | **{r.pass_rate*100:.1f}%** | "
            f"{r.mean_latency_s:.1f}s | {r.p95_latency_s:.1f}s | "
            f"{r.wall_seconds:.0f}s |"
        )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
