"""Per-(tier, capability) parameter auto-tuning for the bench runner.

Goal: discover which sampling parameters maximise pass-rate (subject to
token-cost ceiling) for each (tier, capability) pair, then persist the
result so subsequent bench runs and live chat completions automatically
adopt the best-known config.

The tuner is *online* and *stateful*. Each tuning round picks a small
grid of candidate parameter overlays for the cell, runs N problems per
candidate, scores by Wilson-score lower-bound (so a candidate with
narrow CI beats a noisy higher-mean one), and records the best overlay
to ``data/eval/tuned_params.json``. Subsequent runs of the same cell
read the file and apply the overlay verbatim.

What's tunable
--------------
Only request-level (per-call) parameters appear here. Server-spawn
options (``cache_type_k``, ``parallel_slots``, ``ctx_size``,
``n_gpu_layers``) require respawning llama-server and are out of
scope — those live in ``config/models.yaml`` and are tuned by hand.

Compatible with
---------------
- llama.cpp REST API (the only backend the local stack uses today).
- The schema is conservative enough to forward verbatim to OpenAI-shaped
  endpoints when ``frequency_penalty`` / ``presence_penalty`` are set.

Why Wilson-LCB instead of mean
------------------------------
At the small N values we use during tuning (~30 problems per candidate
to keep wall-time bounded), a single lucky run of 18/30 = 60% can beat
an actually-better candidate at 21/30 = 70% just because the mean
estimator is high-variance. The Wilson 95% lower bound penalises
high-variance candidates correctly: 18/30 has Wilson-LCB ≈ 0.42 while
21/30 has ≈ 0.52. We pick the candidate with the highest Wilson-LCB,
which is the same rule that the early-stop heuristic uses for cell
termination.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


# ── Schema: which parameters can be tuned, valid ranges, default ────────

@dataclass
class ParamSpec:
    name: str
    default: float
    grid: list[float]                  # candidate values for grid search
    min_value: float
    max_value: float
    description: str = ""


# Tunable per-call parameters. Wider grids → longer tuning runs but
# better coverage. Order matters for deterministic Cartesian-product
# expansion: place the most impactful first so a coarse search still
# captures most of the variance.
# Capabilities collapse into broader categories for tuning. Sibling caps
# (e.g. MMLU + MMLU-Pro both fall under ``knowledge``) share their tuned
# overlay because they exercise the same generation pattern. Reduces
# the tuning matrix from O(tiers × capabilities) to O(tiers × categories)
# and makes the optimal overlay actually transfer.
CAPABILITY_CATEGORIES: dict[str, str] = {
    "knowledge":             "knowledge",
    "knowledge_specialized": "knowledge",
    "math":                  "math",
    "math_competition":      "math",
    "reasoning":             "reasoning",
    "coding":                "coding",
    "coding_basic":          "coding",
    "intent":                "tool_use",
    "clarity":               "clarity",
    "long_context":          "long_context",
}

# Representative capability per category — used by the tuner to pick
# which dataset to grid-search on (one cap per category, not all of
# them, to bound tuning wall-time).
CATEGORY_REPRESENTATIVES: dict[str, str] = {
    "knowledge":    "knowledge",        # MMLU is the cheapest
    "math":         "math",             # GSM8K is the cheapest
    "reasoning":    "reasoning",        # AIME is the only one
    "coding":       "coding",           # HumanEval is the cheapest
    "tool_use":     "intent",           # IFEval
    "clarity":      "clarity",          # MT-Bench
    "long_context": "long_context",     # needle
}


def category_of(capability: str) -> str:
    """Map a capability to its tuning category. Falls back to the cap
    name itself when not found (so unknown caps still get isolated
    overlays rather than colliding under a default key)."""
    return CAPABILITY_CATEGORIES.get(capability, capability)


TUNABLE_PARAMS: dict[str, ParamSpec] = {
    "temperature": ParamSpec(
        "temperature", default=0.7,
        grid=[0.0, 0.3, 0.6, 0.9, 1.2],
        min_value=0.0, max_value=2.0,
        description="Sampling temperature. 0 = greedy / deterministic.",
    ),
    "top_p": ParamSpec(
        "top_p", default=0.9,
        grid=[0.7, 0.85, 0.95, 1.0],
        min_value=0.0, max_value=1.0,
        description="Nucleus sampling — keep tokens whose cumulative probability is ≤ top_p.",
    ),
    "top_k": ParamSpec(
        "top_k", default=20,
        grid=[1, 10, 40, 100],
        min_value=1, max_value=500,
        description="Top-K sampling cutoff. 1 = greedy.",
    ),
    "min_p": ParamSpec(
        "min_p", default=0.0,
        grid=[0.0, 0.05, 0.1],
        min_value=0.0, max_value=1.0,
        description="Min-p sampling: floor on the probability mass kept.",
    ),
    "repeat_penalty": ParamSpec(
        "repeat_penalty", default=1.0,
        grid=[1.0, 1.05, 1.1, 1.2],
        min_value=1.0, max_value=2.0,
        description="Multiplier on already-seen tokens. >1 discourages repetition.",
    ),
    "frequency_penalty": ParamSpec(
        "frequency_penalty", default=0.0,
        grid=[0.0, 0.3, 0.6],
        min_value=0.0, max_value=2.0,
        description="OpenAI-style frequency penalty (subtracted from logits).",
    ),
    "presence_penalty": ParamSpec(
        "presence_penalty", default=0.0,
        grid=[0.0, 0.3, 0.6],
        min_value=0.0, max_value=2.0,
        description="OpenAI-style presence penalty (subtracted once if token appeared).",
    ),
}


# ── Storage ──────────────────────────────────────────────────────────────

_PARAMS_FILE_NAME = "tuned_params.json"
_lock = threading.Lock()


def _params_file() -> Path:
    repo = Path(__file__).resolve().parent.parent.parent
    return repo / "data" / "eval" / _PARAMS_FILE_NAME


def load_all() -> dict:
    """Read the entire tuned-params file. Returns a fresh dict on missing
    or unparseable file (never raises)."""
    p = _params_file()
    if not p.exists():
        return {"schema_version": 1, "by_tier_cap": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("tuned_params.json unparseable, treating as empty: %s", exc)
        return {"schema_version": 1, "by_tier_cap": {}}


def save_all(data: dict) -> None:
    """Atomic write — temp file then rename so a crashed mid-write doesn't
    corrupt the on-disk file."""
    p = _params_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def get_overlay(tier: str, capability: str, think: bool = False) -> dict[str, float]:
    """Return the persisted overlay for this tier+think_mode. The
    overlay is shared across all capabilities — we tune for the combo
    that maximises average performance across knowledge/math/coding/
    tool_use, not per-cap. ``capability`` is accepted for API symmetry
    and used only in fallback lookup chains.

    Lookup order: ``by_tier_think`` (current schema) → ``by_tier_category``
    (old per-category overlay) → ``by_tier_cap`` (legacy)."""
    think_label = "on" if think else "off"
    with _lock:
        data = load_all()
    by_tier_think = data.get("by_tier_think", {})
    cat = category_of(capability)
    entry = (
        by_tier_think.get(f"{tier}/think_{think_label}")
        or data.get("by_tier_category", {}).get(f"{tier}/{cat}")
        or data.get("by_tier_cap", {}).get(f"{tier}/{capability}")
        or {}
    )
    return {k: v for k, v in entry.items() if not k.startswith("_")}


def update_overlay(
    tier: str,
    capability: str,
    overlay: dict[str, float],
    *,
    score: float,
    n_samples: int,
    think: bool = False,
    composite_breakdown: dict | None = None,
    explored: list[dict] | None = None,
) -> None:
    """Save the best overlay for (tier, think_mode). Same overlay then
    applies to all capabilities at runtime — the persisted record's
    ``_composite_breakdown`` carries the per-category scores so an
    operator can audit whether the chosen overlay traded off categories
    against each other."""
    think_label = "on" if think else "off"
    key = f"{tier}/think_{think_label}"
    record = {
        **overlay,
        "_think":       think_label,
        "_score":       round(score, 4),
        "_n_samples":   int(n_samples),
        "_last_tuned":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if composite_breakdown:
        record["_composite_breakdown"] = composite_breakdown
    if explored is not None:
        record["_explored"] = explored
    with _lock:
        data = load_all()
        data.setdefault("by_tier_think", {})[key] = record
        save_all(data)


# ── Grid generation ──────────────────────────────────────────────────────

def propose_grid(
    param_names: list[str] | None = None,
    *,
    max_combos: int = 12,
) -> list[dict[str, float]]:
    """Cartesian product of grid values for ``param_names`` (defaults to
    all tunable params). The full Cartesian product can blow up fast
    (5×4×4×3×4×3×3 = 8640), so we sample at most ``max_combos`` from it
    via stratified pick — first the corners (each grid's first / last),
    then a random middle. Always includes the all-defaults overlay as
    the baseline.
    """
    import itertools
    import random

    names = param_names or list(TUNABLE_PARAMS.keys())
    grids = [TUNABLE_PARAMS[n].grid for n in names]
    full = list(itertools.product(*grids))

    if len(full) <= max_combos:
        return [dict(zip(names, combo)) for combo in full]

    # Always include the all-defaults overlay (the actual production setting)
    defaults_combo = tuple(TUNABLE_PARAMS[n].default for n in names)
    chosen = {defaults_combo}

    # Add corners — first and last of each grid (extreme settings)
    corners = [tuple(g[i] for g in grids) for i in (0, -1)]
    for c in corners:
        chosen.add(c)

    # Fill remainder with random sampling
    rng = random.Random(42)  # deterministic
    while len(chosen) < max_combos:
        chosen.add(tuple(rng.choice(g) for g in grids))

    return [dict(zip(names, combo)) for combo in list(chosen)[:max_combos]]


# ── Wilson-score lower bound (scoring rule for picking best candidate) ──

def wilson_lcb(passed: int, n: int, z: float = 1.96) -> float:
    """Wilson 95% lower bound. ``passed`` successes out of ``n`` trials.
    Returns 0.0 when n=0. Used as the candidate-selection metric so a
    high-variance lucky run doesn't beat a tighter result."""
    if n <= 0:
        return 0.0
    p = passed / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))) / denom
    return max(0.0, centre - half)
