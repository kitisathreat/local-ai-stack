"""Dataset loaders. Each returns a list of Problem dicts.

Depth controls how many problems are sampled:
    fast    — 15-30 per dataset (~30 min/tier across all capabilities)
    medium  — 50-150 per dataset (~2 hr/tier)
    full    — entire dataset (varies; see __init__.py)

Sampling is deterministic (Random(42)) so two runs at the same depth
hit the same questions — A/B comparisons across tier configs stay clean.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pandas as pd


def _datasets_dir() -> Path:
    """Repo-relative path to the vendored datasets. Resolves from this
    file rather than CWD so the loader works whether eval_tiers.py is
    invoked from the repo root or from scripts/."""
    return Path(__file__).resolve().parent.parent.parent / "data" / "eval" / "datasets"


Depth = Literal["fast", "medium", "full"]
# Per-dataset sample sizes per depth. Tuned so a "fast" run completes
# in ~30 minutes per tier on a 25 tok/s mid-tier; "medium" gives more
# statistical confidence; "full" is for the overnight definitive bench.
_DEPTHS: dict[str, dict[Depth, int]] = {
    "humaneval":  {"fast": 30,  "medium": 80,  "full": 164},   # full == all problems
    "gsm8k":      {"fast": 50,  "medium": 200, "full": 1319},
    "aime2024":   {"fast": 15,  "medium": 30,  "full": 30},    # only ~30 exist
    "mmlu":       {"fast": 50,  "medium": 150, "full": 399},   # 399 is the vendored subset
    "needle":     {"fast": 4,   "medium": 8,   "full": 16},    # ctx-length × depth
}


@dataclass
class Problem:
    """One question from a benchmark.

    `kind` distinguishes graders. `prompt` is the literal text fed to the
    model. `answer` is the canonical expected answer (format depends on
    grader: int for AIME/GSM8K, multiple-choice letter for MMLU, list of
    test-strings for HumanEval). `meta` carries grader-specific extras."""
    kind: str
    id: str
    prompt: str
    answer: Any
    meta: dict[str, Any]


def _sample(rows: list, n: int, seed: int = 42) -> list:
    if n >= len(rows):
        return list(rows)
    rng = random.Random(seed)
    idx = list(range(len(rows)))
    rng.shuffle(idx)
    return [rows[i] for i in sorted(idx[:n])]


# ── HumanEval ─────────────────────────────────────────────────────────────

def load_humaneval(depth: Depth = "fast") -> list[Problem]:
    """OpenAI HumanEval — 164 Python function-completion problems. The
    model receives the function signature + docstring and must complete
    the body so that the canonical unit tests pass.

    The grader runs the completed code in a subprocess sandbox (10 s
    timeout, no network). HumanEval is the only grader that executes
    untrusted model output — see `graders.score_humaneval`."""
    df = pd.read_parquet(_datasets_dir() / "humaneval" / "HumanEval.parquet")
    rows = df.to_dict("records")
    n = _DEPTHS["humaneval"][depth]
    sampled = _sample(rows, n)
    out: list[Problem] = []
    for row in sampled:
        out.append(Problem(
            kind="humaneval",
            id=str(row["task_id"]),
            # HumanEval prompts are already in the form
            #   def func(...):\n    """docstring..."""\n
            # so the model just continues from there.
            prompt=row["prompt"],
            # Canonical solution + test code, both used by the grader.
            answer={
                "test": row["test"],
                "entry_point": row["entry_point"],
                "canonical": row["canonical_solution"],
            },
            meta={"task_id": row["task_id"]},
        ))
    return out


# ── GSM8K ─────────────────────────────────────────────────────────────────

def load_gsm8k(depth: Depth = "fast") -> list[Problem]:
    """Grade-school math word problems. Each answer ends with `#### N`
    where N is the integer answer; we strip that to leave the question."""
    df = pd.read_parquet(_datasets_dir() / "gsm8k" / "test.parquet")
    rows = df.to_dict("records")
    n = _DEPTHS["gsm8k"][depth]
    sampled = _sample(rows, n)
    out: list[Problem] = []
    for i, row in enumerate(sampled):
        # answer field looks like:  "Step 1: ...\nStep 2: ...\n#### 42"
        # We want the integer after #### as the canonical answer.
        full_answer = str(row["answer"])
        try:
            canonical = int(full_answer.rsplit("####", 1)[1].strip().replace(",", ""))
        except (IndexError, ValueError):
            canonical = None  # malformed — grader will skip
        out.append(Problem(
            kind="gsm8k",
            id=f"gsm8k-{i:04d}",
            prompt=(
                "Solve the following grade-school math problem. Give your "
                "final answer as a single integer on the last line, prefixed "
                "by '####'. Show your work first.\n\n" + str(row["question"])
            ),
            answer=canonical,
            meta={"reasoning_trace": full_answer},
        ))
    return out


# ── AIME 2024 ─────────────────────────────────────────────────────────────

def load_aime2024(depth: Depth = "fast") -> list[Problem]:
    """American Invitational Mathematics Exam 2024 — 30 hard math
    olympiad problems with integer answers in [0, 999]. Strong reasoning
    benchmark; even Qwen3-Next-80B-Thinking only solves ~50% at
    `think_default: true`."""
    df = pd.read_parquet(_datasets_dir() / "aime2024" / "aime2024.parquet")
    rows = df.to_dict("records")
    n = _DEPTHS["aime2024"][depth]
    sampled = _sample(rows, n)
    out: list[Problem] = []
    # Column names vary by HF dataset; the Maxwell-Jia upload uses
    # "Problem" / "Answer" — be defensive in case it changes upstream.
    prob_col = next((c for c in ("Problem", "problem", "question") if c in df.columns), None)
    ans_col = next((c for c in ("Answer", "answer") if c in df.columns), None)
    if not prob_col or not ans_col:
        raise RuntimeError(
            f"AIME parquet schema unexpected. Columns: {list(df.columns)}"
        )
    for i, row in enumerate(sampled):
        try:
            canonical = int(str(row[ans_col]).strip())
        except (ValueError, TypeError):
            canonical = None
        out.append(Problem(
            kind="aime2024",
            id=f"aime2024-{i:02d}",
            prompt=(
                "Solve the following AIME problem. The answer is an integer "
                "between 0 and 999 inclusive. Give your final answer as a "
                "single integer on the last line, prefixed by '####'. Show "
                "your full reasoning first.\n\n" + str(row[prob_col])
            ),
            answer=canonical,
            meta={},
        ))
    return out


# ── MMLU subset ───────────────────────────────────────────────────────────

def load_mmlu(depth: Depth = "fast") -> list[Problem]:
    """MMLU multiple-choice across 57 academic subjects. Each row has
    question + 4 choices + index of correct choice. Stratified sample
    (~7 per subject) is vendored — see datasets/README.md."""
    df = pd.read_parquet(_datasets_dir() / "mmlu_subset" / "mmlu_subset.parquet")
    rows = df.to_dict("records")
    n = _DEPTHS["mmlu"][depth]
    sampled = _sample(rows, n)
    letters = ["A", "B", "C", "D"]
    out: list[Problem] = []
    for i, row in enumerate(sampled):
        choices = list(row["choices"])
        choices_block = "\n".join(f"  {l}. {c}" for l, c in zip(letters, choices))
        out.append(Problem(
            kind="mmlu",
            id=f"mmlu-{row['subject']}-{i:04d}",
            prompt=(
                f"Subject: {row['subject']}\n\n"
                f"Question: {row['question']}\n\n"
                f"Choices:\n{choices_block}\n\n"
                "Respond with the single letter (A, B, C, or D) of the "
                "correct choice on the last line, prefixed by '####'."
            ),
            answer=letters[int(row["answer"])],
            meta={"subject": str(row["subject"])},
        ))
    return out


# ── Needle in haystack ────────────────────────────────────────────────────

# Distinct, easy-to-grep needle phrases — picked so they don't accidentally
# appear in any of the haystack filler text. The grader looks for the
# *answer* (the secret number) literally in the response; the question
# asks "what is the secret number".
_NEEDLE_FILLER = (
    "The wind blew softly through the pines as Aria walked along the trail "
    "she had walked a hundred times before. The path led down through "
    "moss-covered rocks toward the stream that fed the lake below. Birds "
    "called in the distance, their songs echoing off the canyon walls. "
    "She stopped to drink from the cold spring water, watching small fish "
    "dart between the stones. The afternoon light filtered through the "
    "leaves, casting dappled shadows on the forest floor. "
)


def load_needle(depth: Depth = "fast") -> list[Problem]:
    """Synthetic needle-in-haystack: hide a secret number inside a long
    block of filler text and ask the model to retrieve it. Tests the
    long-context recall path that's specific to this rig (KV-on-CPU,
    YaRN scaling factors, ctx-shrink cascade).

    `fast` depth tests at 4 ctx lengths × 1 needle position each.
    `medium` adds 4 needle positions (early/mid/late/random).
    `full` runs all 16 combinations (4 ctx × 4 positions)."""
    if depth == "fast":
        ctxs = [4096, 16384, 32768, 65536]
        positions = [0.5]   # mid only
    elif depth == "medium":
        ctxs = [4096, 16384, 32768, 65536]
        positions = [0.1, 0.5, 0.9]
    else:
        ctxs = [4096, 16384, 32768, 65536]
        positions = [0.1, 0.3, 0.5, 0.7, 0.9]

    rng = random.Random(42)
    out: list[Problem] = []
    chunk_tokens = 25  # rough — _NEEDLE_FILLER is ~110 chars / ~25 tok
    for ctx_target in ctxs:
        # Each ctx target needs (ctx_target / chunk_tokens) repeats of filler
        # to roughly fill the window. Leave 200 tokens of headroom for the
        # question + answer + system prompt.
        n_chunks = max(1, (ctx_target - 200) // chunk_tokens)
        for pos in positions:
            secret = rng.randint(100000, 999999)
            insert_at = int(n_chunks * pos)
            chunks = [_NEEDLE_FILLER] * n_chunks
            chunks[insert_at] = (
                f" The secret number is {secret}. Remember it carefully. "
            )
            haystack = "".join(chunks)
            out.append(Problem(
                kind="needle",
                id=f"needle-ctx{ctx_target}-pos{int(pos*100):02d}",
                prompt=(
                    "Read the following text carefully. Somewhere in it is a "
                    "sentence stating a secret number. After reading, report "
                    "the secret number as a single integer on the last line, "
                    "prefixed by '####'.\n\n--- BEGIN TEXT ---\n"
                    + haystack +
                    "\n--- END TEXT ---\n\nWhat is the secret number?"
                ),
                answer=secret,
                meta={"ctx_target": ctx_target, "position": pos},
            ))
    return out
