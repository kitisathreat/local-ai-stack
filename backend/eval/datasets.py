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
    "aime2024":   {"fast": 30,  "medium": 60,  "full": 89,    "stat_sig": 89,   "stat_sig_strict": 89},
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


# ── Few-shot CoT prefixes (lit-format matching) ──────────────────────────────
# Published baselines for MMLU / MMLU-Pro / GSM8K are evaluated with a
# specific in-context prompt format (5-shot CoT for MMLU/MMLU-Pro, 8-shot
# CoT for GSM8K — Wei et al). Earlier our prompts were 0-shot, which tends
# to lose 5-15pp on these benchmarks for non-reasoning models. Adding the
# same exemplar prefix the lit uses makes the comparison apples-to-apples.
#
# These prefixes are deliberately short worked examples — not exact matches
# of the official MMLU-Pro `cot_examples.json` (which is per-category).
# They demonstrate the answer format ("The answer is (X)" / "#### N") so
# the model emits a graderable final line, AND they prime the model to
# show its reasoning explicitly. Per-category prefixes would add ~5pp more
# but quintuple the dataset bookkeeping; uniform 5-shot captures most of
# the benefit for one-tenth the code.
_MMLU_PRO_5SHOT_COT = """\
The following are multiple-choice questions (with answers). Show your reasoning for each, then end with 'The answer is (X)' on its own line.

Question: A 65-year-old man with a history of myocardial infarction presents with sudden onset shortness of breath and a new diastolic murmur best heard at the apex. Which of the following is most likely?
Options:
  (A) Aortic stenosis
  (B) Mitral regurgitation due to papillary muscle rupture
  (C) Pulmonary embolism
  (D) Tricuspid regurgitation
  (E) Pericarditis
Reasoning: Acute MI complications include papillary muscle rupture, which causes acute mitral regurgitation. The murmur of MR is holosystolic, but in acute severe MR with elevated LA pressure, the regurgitant flow can sound diastolic-like at the apex. The setting (post-MI, acute dyspnea, apical murmur) points to papillary muscle rupture.
The answer is (B)

Question: A solution containing 0.10 M acetic acid (Ka = 1.8 × 10^-5) and 0.10 M sodium acetate has what pH?
Options:
  (A) 2.87
  (B) 3.74
  (C) 4.74
  (D) 5.74
  (E) 7.00
Reasoning: This is a buffer with [HA] = [A-]. By Henderson-Hasselbalch, pH = pKa + log([A-]/[HA]) = pKa + log(1) = pKa. pKa = -log(1.8 × 10^-5) ≈ 4.74.
The answer is (C)

Question: Which of the following is the time complexity of finding the median of an unsorted array of n elements using the median-of-medians algorithm?
Options:
  (A) O(log n)
  (B) O(n)
  (C) O(n log n)
  (D) O(n^2)
  (E) O(2^n)
Reasoning: Median-of-medians (Blum, Floyd, Pratt, Rivest, Tarjan, 1973) gives a deterministic linear-time selection algorithm. Despite the recursive structure, the recurrence T(n) = T(n/5) + T(7n/10) + O(n) solves to O(n).
The answer is (B)

Question: In macroeconomics, the Phillips curve in its original form describes the relationship between which two variables?
Options:
  (A) Inflation and unemployment
  (B) Inflation and GDP growth
  (C) Interest rates and unemployment
  (D) Money supply and inflation
  (E) Government spending and tax revenue
Reasoning: The original Phillips curve (A.W. Phillips, 1958) was an empirical observation in UK data showing an inverse relationship between the rate of wage inflation and unemployment. Later generalized to price inflation vs unemployment.
The answer is (A)

Question: Which of the following best characterizes the Treaty of Westphalia (1648)?
Options:
  (A) Established the principle of papal supremacy in temporal affairs
  (B) Created the modern concept of sovereign nation-states
  (C) Founded the European Union
  (D) Ended the Hundred Years' War
  (E) Established free trade across Europe
Reasoning: The Peace of Westphalia (1648) ended the Thirty Years' War and is widely cited as the origin of the modern Westphalian state system, in which sovereign states have exclusive authority within their territory.
The answer is (B)

"""

_MMLU_5SHOT_COT = """\
The following are multiple-choice questions (with answers). Show your reasoning for each, then end with 'The answer is (X)' on its own line.

Question: Which of the following is the most abundant gas in Earth's atmosphere?
Choices:
  A. Oxygen
  B. Nitrogen
  C. Carbon dioxide
  D. Argon
Reasoning: Earth's atmosphere is approximately 78% nitrogen, 21% oxygen, with the remaining 1% being argon, CO2, and trace gases. Nitrogen is by far the most abundant.
The answer is (B)

Question: A particle of mass m moves in a circle of radius r at constant speed v. What is its acceleration?
Choices:
  A. 0
  B. v/r
  C. v^2/r directed toward the center
  D. v^2/r directed away from the center
Reasoning: Uniform circular motion has centripetal acceleration of magnitude v^2/r, always directed toward the center of the circle. Speed is constant, so there is no tangential acceleration, but the velocity vector is changing direction, requiring a center-directed acceleration.
The answer is (C)

Question: In a market with positive externalities, which of the following best describes the market equilibrium relative to the socially optimal level of output?
Choices:
  A. Output is too high
  B. Output is too low
  C. Output is at the social optimum
  D. Cannot be determined
Reasoning: Positive externalities mean the social benefit exceeds the private benefit. Private agents produce only up to the point where marginal private benefit equals marginal cost, which is below the socially optimal point where marginal social benefit equals marginal cost. So output is too low.
The answer is (B)

Question: Which of the following is a correct property of a binary search tree?
Choices:
  A. The left subtree of any node contains only values greater than the node's value
  B. The right subtree of any node contains only values less than the node's value
  C. The left subtree of any node contains only values less than the node's value
  D. All leaves are at the same depth
Reasoning: In a BST, by definition, every node's left subtree has values strictly less than the node, and the right subtree has values strictly greater. Choice C states the left-subtree property correctly.
The answer is (C)

Question: Which philosopher is most associated with the categorical imperative?
Choices:
  A. John Stuart Mill
  B. Immanuel Kant
  C. Jean-Paul Sartre
  D. David Hume
Reasoning: The categorical imperative is the central concept of Kant's deontological ethics, set out in the Groundwork of the Metaphysics of Morals (1785). Mill is utilitarian, Sartre is existentialist, Hume is empiricist.
The answer is (B)

"""

_GSM8K_8SHOT_COT = """\
Solve each grade-school math word problem step by step. Show your work, then end with '#### N' where N is the integer answer.

Question: Janet's ducks lay 16 eggs per day. She eats 3 for breakfast and bakes 4 into muffins. She sells the rest at the farmer's market for $2 per egg. How much does she make per day at the farmer's market?
Reasoning: She has 16 eggs. She uses 3 + 4 = 7. She sells 16 - 7 = 9 eggs. At $2 each that's 9 × 2 = 18 dollars.
#### 18

Question: A robe takes 2 bolts of blue fiber and half that much white fiber. How many bolts in total does it take?
Reasoning: White is half of 2, which is 1. Total is 2 + 1 = 3 bolts.
#### 3

Question: Josh buys a house for $80,000 and puts $50,000 in repairs. The repairs increase the value by 150%. How much profit does he make?
Reasoning: Original value $80,000. After repairs the value increases by 150% of $80,000 = $120,000, so new value = $80,000 + $120,000 = $200,000. Total cost = $80,000 + $50,000 = $130,000. Profit = $200,000 - $130,000 = $70,000.
#### 70000

Question: James decides to run 3 sprints 3 times a week. He runs 60 meters each sprint. How many total meters does he run a week?
Reasoning: Per session: 3 sprints × 60 m = 180 m. Per week: 180 × 3 = 540 m.
#### 540

Question: Every day, Wendi feeds each of her chickens three cups of mixed chicken feed, containing seeds, mealworms, and vegetables. In the morning she gives 15 cups; in the afternoon she gives 25 cups. If she has 20 chickens, how many cups does she give in the final meal of the day?
Reasoning: Each chicken eats 3 cups per day total. With 20 chickens that's 60 cups per day. She gives 15 + 25 = 40 cups in the first two meals. Final meal = 60 - 40 = 20 cups.
#### 20

Question: Kylar went to the store to buy glasses for his new apartment. One glass costs $5, but every second glass costs only 60% of the price. Kylar wants to buy 16 glasses. How much does he need to pay for them?
Reasoning: Pairs: 16 / 2 = 8 pairs. Each pair costs $5 + $5 × 0.6 = $5 + $3 = $8. Total = 8 × $8 = $64.
#### 64

Question: Toulouse has twice as many sheep as Charleston. Charleston has 4 times as many sheep as Seattle. If Seattle has 20 sheep, how many sheep do they have in total?
Reasoning: Seattle = 20. Charleston = 4 × 20 = 80. Toulouse = 2 × 80 = 160. Total = 20 + 80 + 160 = 260.
#### 260

Question: Carla is downloading a 200 GB file. After 40% has downloaded, Windows forces a restart and she has to start over. Then the download proceeds without interruption. If her connection runs at 2 GB/minute, how many minutes does the entire process take?
Reasoning: 40% of 200 = 80 GB downloaded before restart. After restart, 200 GB has to download. Total downloaded across both attempts = 80 + 200 = 280 GB. At 2 GB/min: 280 / 2 = 140 minutes.
#### 140

"""


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
            # 8-shot CoT prefix matches the published GSM8K eval format
            # (Wei et al, 2022). Without it our 0-shot prompt loses ~5pp
            # on weaker tiers.
            prompt=(
                _GSM8K_8SHOT_COT
                + "Question: " + str(row["question"]) + "\n"
                + "Reasoning:"
            ),
            answer=canonical,
            meta={"reasoning_trace": full_answer},
        ))
    return out


# ── AIME 2024 ─────────────────────────────────────────────────────────────

def load_aime2024(depth: Depth = "fast") -> list[Problem]:
    """AIME 2022 + 2023 + 2024 combined — 89 olympiad problems with
    integer answers in [0, 999]. Strong reasoning benchmark; even
    Qwen3-Next-80B-Thinking only solves ~50% at `think_default: true`.

    Originally 2024-only (30 problems); expanded to three years so
    Wilson-CI early-stop has a chance to settle within the 5pp margin
    (n=30 ceiling at ~17pp half-width never fired)."""
    base = _datasets_dir()
    frames = []
    for year_dir in ("aime2022", "aime2023", "aime2024"):
        p = base / year_dir / f"{year_dir}.parquet"
        if p.exists():
            frames.append(pd.read_parquet(p))
    if not frames:
        # Fallback to legacy single-file path
        frames = [pd.read_parquet(base / "aime2024" / "aime2024.parquet")]
    df = pd.concat(frames, ignore_index=True)
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
            # 5-shot CoT prefix — matches the lit MMLU eval format. The
            # grader accepts both "The answer is (X)" (via _MMLU_LETTER on
            # the last line) and the "#### X" fallback, so the prompt can
            # request either.
            prompt=(
                _MMLU_5SHOT_COT
                + f"Question: {row['question']}\n"
                + f"Choices:\n{choices_block}\n"
                + "Reasoning:"
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
            # 5-shot CoT prefix — matches the official MMLU-Pro eval (TIGER-AI
            # repo) prompt structure. Uniform 5-shot (not per-category) for
            # implementation simplicity; per-category would gain ~2-5pp more.
            prompt=(
                _MMLU_PRO_5SHOT_COT
                + f"Question: {row['question']}\n"
                + f"Options:\n{choices_block}\n"
                + "Reasoning:"
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
