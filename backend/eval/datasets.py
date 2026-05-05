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


Depth = Literal["fast", "medium", "full", "stat_sig", "stat_sig_strict"]
# Per-dataset sample sizes per depth. Tuned so a "fast" run completes
# in ~30 minutes per tier on a 25 tok/s mid-tier; "medium" gives more
# statistical confidence; "full" is for the overnight definitive bench.
_DEPTHS: dict[str, dict[Depth, int]] = {
    "humaneval":  {"fast": 30,  "medium": 80,  "full": 164,   "stat_sig": 164,  "stat_sig_strict": 164},
    "gsm8k":      {"fast": 50,  "medium": 200, "full": 1319,  "stat_sig": 200,  "stat_sig_strict": 385},
    "aime2024":   {"fast": 15,  "medium": 30,  "full": 30,    "stat_sig": 30,   "stat_sig_strict": 30},
    "mmlu":       {"fast": 50,  "medium": 150, "full": 399,   "stat_sig": 200,  "stat_sig_strict": 385},
    "needle":     {"fast": 4,   "medium": 8,   "full": 16,    "stat_sig": 16,   "stat_sig_strict": 16},
    "mmlu_pro":   {"fast": 100, "medium": 300, "full": 12032, "stat_sig": 200,  "stat_sig_strict": 385},
    "math":       {"fast": 50,  "medium": 150, "full": 367,   "stat_sig": 200,  "stat_sig_strict": 367},
    "math_hard":  {"fast": 30,  "medium": 80,  "full": 134,   "stat_sig": 134,  "stat_sig_strict": 134},
    "ifeval":     {"fast": 50,  "medium": 200, "full": 541,   "stat_sig": 200,  "stat_sig_strict": 385},
    "mbpp":       {"fast": 30,  "medium": 100, "full": 257,   "stat_sig": 200,  "stat_sig_strict": 257},
    "mtbench":    {"fast": 30,  "medium": 60,  "full": 80,    "stat_sig": 80,   "stat_sig_strict": 80},
}
# stat_sig depth: N=200 per cell → ±7pp at 95% CI for single-cell point
#                 estimate (z=1.96, p=0.5 worst case), and detects an
#                 ~8pp difference between two cells at α=0.05 / power 0.80.
# stat_sig_strict: N=385 → ±5pp at 95% CI. Doubles wall time of the
#                  larger-dataset cells. Smaller-dataset cells (AIME=30,
#                  HumanEval=164, needle=16) reuse their full count
#                  since those are the entire dataset.


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
    # _NEEDLE_FILLER is ~457 chars / ~110 tokens (measured against Qwen
    # and Granite BPE tokenizers; English averages ~3.8 chars/token).
    # The previous estimate of 25 tokens/chunk was off by 4.4×, which
    # produced ~17.6k-token prompts under a "ctx_target=4096" label and
    # silently overflowed swarm's 16k context — the model returned
    # empty content and the runner aborted with consecutive_zero_tok
    # on every needle cell.
    #
    # We pad chunk_tokens up by ~10% (110 → 125) because real BPE
    # tokenizers come in slightly lower than the chars/3.8 estimate
    # for the boring repeating filler text, and the runner's per-tier
    # ctx_skip filter compares ctx_target verbatim to the tier window.
    # Aiming for 90-95% utilisation leaves headroom for the question
    # text + system prompt + tier-specific BOS/EOS overhead.
    chunk_tokens = 125
    for ctx_target in ctxs:
        # n_chunks = roughly (ctx_target × 0.9) / chunk_tokens — fills to
        # ~90% of the labeled ctx_target, leaving ~10% of the window for
        # prompt overhead, the question, the answer, and tokenizer slop.
        n_chunks = max(1, int(ctx_target * 0.9) // chunk_tokens)
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


# ── MMLU-Pro — specialized knowledge, 10-choice ─────────────────────────────

def load_mmlu_pro(depth: Depth = "fast") -> list[Problem]:
    """MMLU-Pro: 12K specialized-domain multiple-choice. Each row has up to
    10 options (A-J), `answer` is the letter, `category` is one of 14
    domains (biology, business, chemistry, ...). Stratified sample so
    each category gets ~equal weight at fast/medium depths."""
    df = pd.read_parquet(_datasets_dir() / "mmlu_pro" / "test.parquet")
    rows = df.to_dict("records")
    n = _DEPTHS["mmlu_pro"][depth]
    sampled = _sample(rows, n)
    letters = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
    out: list[Problem] = []
    for i, row in enumerate(sampled):
        choices = list(row["options"])[:10]
        choices_block = "\n".join(
            f"  {l}. {c}" for l, c in zip(letters, choices) if c
        )
        out.append(Problem(
            kind="mmlu_pro",
            id=f"mmlu_pro-{row['category']}-{i:04d}",
            prompt=(
                f"Domain: {row['category']}\n\n"
                f"Question: {row['question']}\n\n"
                f"Choices:\n{choices_block}\n\n"
                "Respond with the single letter of the correct choice on "
                "the last line, prefixed by '####'."
            ),
            answer=str(row["answer"]).strip().upper(),
            meta={"category": str(row["category"])},
        ))
    return out


# ── MATH — Hendrycks competition mathematics, levels 3-5 ────────────────────

def load_math(depth: Depth = "fast") -> list[Problem]:
    """MATH (Hendrycks): hard competition math problems. Answers can be
    integers, fractions (e.g. ``\\frac{1}{2}``), or LaTeX expressions
    (e.g. ``\\sqrt{3}``). The grader accepts any answer that matches the
    canonical form after light normalisation. We vendor only levels 3-5
    (1-2 overlap with GSM8K)."""
    df = pd.read_parquet(_datasets_dir() / "math" / "test.parquet")
    rows = df.to_dict("records")
    n = _DEPTHS["math"][depth]
    sampled = _sample(rows, n)
    out: list[Problem] = []
    for i, row in enumerate(sampled):
        out.append(Problem(
            kind="math",
            id=f"math-L{row['level']}-{i:04d}",
            prompt=(
                "Solve the following competition mathematics problem. The "
                "answer may be a number, fraction, or simple LaTeX "
                "expression. Show your work, then give your final answer "
                "on the last line wrapped in \\boxed{...}.\n\n"
                + str(row["problem"])
            ),
            answer=str(row["answer"]),
            meta={
                "level": int(row["level"]),
                "subject": str(row["subject"]),
            },
        ))
    return out


# ── MATH-Hard — Level 5 only, the hardest competition problems ─────────────

def load_math_hard(depth: Depth = "fast") -> list[Problem]:
    """MATH (Hendrycks) restricted to Level 5 — the hardest competition
    problems in the dataset. Sits between MATH (levels 3-5 mixed; saturates
    at the coding+ tiers) and AIME (30 problems, very narrow). At ~134
    problems with Level-5 difficulty, top tiers reliably score ~70-80%
    and weaker tiers ~10-30%, giving the bench cleaner tier separation
    on the math axis."""
    df = pd.read_parquet(_datasets_dir() / "math" / "test.parquet")
    df_hard = df[df["level"] == 5]
    rows = df_hard.to_dict("records")
    n = _DEPTHS["math_hard"][depth]
    sampled = _sample(rows, n)
    out: list[Problem] = []
    for i, row in enumerate(sampled):
        out.append(Problem(
            kind="math",   # reuse the math grader (LaTeX/integer/fraction tolerant)
            id=f"math-L5-{i:04d}",
            prompt=(
                "Solve the following competition mathematics problem. The "
                "answer may be a number, fraction, or simple LaTeX "
                "expression. Show your work, then give your final answer "
                "on the last line wrapped in \\boxed{...}.\n\n"
                + str(row["problem"])
            ),
            answer=str(row["answer"]),
            meta={
                "level": int(row["level"]),
                "subject": str(row["subject"]),
            },
        ))
    return out


# ── IFEval — instruction-following with verifiable constraints ──────────────

def load_ifeval(depth: Depth = "fast") -> list[Problem]:
    """IFEval: 541 prompts where each instruction encodes a machine-
    checkable constraint (e.g. "respond in exactly 3 paragraphs",
    "use at least 5 keywords from this list"). The grader verifies the
    response satisfies ALL declared instruction_id constraints. The
    `kwargs` field carries any per-instruction parameters."""
    df = pd.read_parquet(_datasets_dir() / "ifeval" / "test.parquet")
    rows = df.to_dict("records")
    n = _DEPTHS["ifeval"][depth]
    sampled = _sample(rows, n)
    out: list[Problem] = []
    for i, row in enumerate(sampled):
        # parquet -> dict can yield numpy arrays for list columns; coerce
        # explicitly so downstream ``or []`` checks don't trip on numpy
        # truthiness ambiguity.
        instr_raw = row.get("instruction_id_list")
        instr_list: list = list(instr_raw) if instr_raw is not None else []
        kwargs_raw = row.get("kwargs")
        kwargs_list: list = list(kwargs_raw) if kwargs_raw is not None else []
        out.append(Problem(
            kind="ifeval",
            id=f"ifeval-{row['key']}",
            prompt=str(row["prompt"]),
            answer={
                "instruction_id_list": instr_list,
                "kwargs": kwargs_list,
            },
            meta={"key": int(row["key"])},
        ))
    return out


# ── MBPP — basic Python programming problems ───────────────────────────────

def load_mbpp(depth: Depth = "fast") -> list[Problem]:
    """MBPP (sanitized): 257 entry-level Python problems. The model is
    given a natural-language description plus 1-3 sample asserts; the
    grader runs the model's code against the held-out `test_list`."""
    df = pd.read_parquet(_datasets_dir() / "mbpp" / "test.parquet")
    rows = df.to_dict("records")
    n = _DEPTHS["mbpp"][depth]
    sampled = _sample(rows, n)
    out: list[Problem] = []
    for i, row in enumerate(sampled):
        # parquet -> dict can yield numpy arrays for list columns;
        # coerce explicitly to avoid numpy truthiness ambiguity.
        tests_raw = row.get("test_list")
        tests = list(tests_raw) if tests_raw is not None else []
        sample_assert = tests[0] if tests else ""
        out.append(Problem(
            kind="mbpp",
            id=f"mbpp-{row['task_id']}",
            prompt=(
                "Write a Python function that solves the problem below. "
                "Wrap your final code in a ```python fence. Make sure it "
                "passes this sample test:\n"
                f"  {sample_assert}\n\n"
                "Problem:\n" + str(row["prompt"])
            ),
            answer={
                "test_list": tests,
                "test_setup_code": str(row.get("test_setup_code") or ""),
                "code": str(row.get("code") or ""),
            },
            meta={"task_id": int(row["task_id"])},
        ))
    return out


# ── MT-Bench — LLM-as-judge clarity scoring ────────────────────────────────

def load_mtbench(depth: Depth = "fast") -> list[Problem]:
    """MT-Bench: 80 prompts spanning 8 categories (writing, roleplay,
    reasoning, math, coding, extraction, stem, humanities). Single-turn
    only — we feed turn1 to the tier under test, then a *judge* model
    scores the response 1-10 for clarity, helpfulness, and depth. The
    grader's threshold is `score >= 7` = PASS. Reference-based prompts
    (math, reasoning, coding) get the canonical `reference1` answer in
    the judge's prompt for grounded scoring."""
    df = pd.read_parquet(_datasets_dir() / "mtbench" / "prompts.parquet")
    rows = df.to_dict("records")
    n = _DEPTHS["mtbench"][depth]
    sampled = _sample(rows, n)
    out: list[Problem] = []
    for i, row in enumerate(sampled):
        out.append(Problem(
            kind="mtbench",
            id=f"mtbench-{row['question_id']}-{row['category']}",
            prompt=str(row["turn1"]),
            answer={
                "category": str(row["category"]),
                "reference": str(row.get("reference1") or ""),
            },
            meta={
                "question_id": int(row["question_id"]),
                "category": str(row["category"]),
            },
        ))
    return out
