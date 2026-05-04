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
    load_mmlu,
    load_needle,
)
from .graders import score


logger = logging.getLogger("backend.eval")


# Capability → loader. Order matters: reasoning + coding first so the slow
# tiers' painful prompts hit the wall-clock deadline first if we hit it.
CAPABILITIES: dict[str, Callable[[Depth], list[Problem]]] = {
    "reasoning": load_aime2024,
    "coding": load_humaneval,
    "math": load_gsm8k,
    "knowledge": load_mmlu,
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
    problems: list[ProblemResult] = field(default_factory=list)

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
) -> tuple[str, int, float]:
    """One chat completion. Returns (text, output_token_estimate, wall_s).

    We use streaming so a hung tier shows up as a slow per-line read
    rather than a single 600 s timeout. Token count is whitespace-split
    on the response — same proxy used by `bench_tiers.py`."""
    body = {
        "model": tier_id,
        "stream": True,
        "think": think,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        api_base.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", "accept": "text/event-stream"},
    )
    started = time.time()
    chunks: list[str] = []
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
                if content:
                    chunks.append(content)
    text = "".join(chunks)
    return text, len(text.split()), time.time() - started


# ── Main entry: run one (tier, capability) cell ──────────────────────────

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
) -> TierResult:
    """Run all problems in `capability` against `tier`, return aggregated
    results. `deadline` (unix ts) lets the caller cap wall time —
    problems past the deadline are skipped + recorded as 'deadline'.

    `think` defaults to True for reasoning + math (chain-of-thought helps),
    False for the rest (knowledge / coding / long_context — recall-style).
    """
    loader = CAPABILITIES[capability]
    problems = loader(depth)
    if think is None:
        think = capability in {"reasoning", "math"}

    logger.info(
        "eval-cell start tier=%s capability=%s depth=%s n=%d think=%s",
        tier, capability, depth, len(problems), think,
    )
    started = time.time()
    results: list[ProblemResult] = []
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
            text, n_tok, wall = _chat(
                api_base, f"tier.{tier}", problem.prompt,
                max_tokens=max_tokens, think=think,
                timeout=per_problem_timeout,
            )
            passed = score(problem, text)
            results.append(ProblemResult(
                id=problem.id, kind=problem.kind, passed=passed,
                latency_s=wall, output_tokens=n_tok,
                output_text_len=len(text), output_preview=text[:200],
            ))
            logger.info(
                "  %s/%d %s tok=%d in %.1fs %s",
                f"{i+1:03d}", len(problems), problem.id, n_tok, wall,
                "PASS" if passed else "fail",
            )
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
