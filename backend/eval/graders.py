"""Per-capability graders. Each takes (problem, model_output) and returns
a bool — passed or not. Stateless; no side effects except for the
HumanEval grader which spawns a subprocess to execute the candidate code.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .datasets import Problem


# ── Answer extraction ─────────────────────────────────────────────────────

# Most graders use the "#### N" trailing answer convention. Be liberal
# on whitespace + accept "Answer: N" and "**Answer**: N" as fallbacks
# since smaller models sometimes drift from the requested format.
_ANSWER_PATTERNS = [
    re.compile(r"####\s*([\-A-Za-z0-9.,/]+)\s*$", re.MULTILINE),
    re.compile(r"\*?\*?Answer\*?\*?:\s*([\-A-Za-z0-9.,/]+)\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"\\boxed\{([^}]+)\}"),  # LaTeX-boxed answers from reasoning models
]


def _extract_answer(text: str) -> str | None:
    """Pull the last ####-prefixed (or fallback) answer string. Returns
    None if no convention matched — grader treats that as a failure."""
    for pat in _ANSWER_PATTERNS:
        matches = pat.findall(text)
        if matches:
            return matches[-1].strip()
    return None


# ── Integer-answer graders (GSM8K, AIME, needle) ──────────────────────────

def _grade_integer(problem: Problem, output: str) -> bool:
    if problem.answer is None:
        return False
    raw = _extract_answer(output)
    if raw is None:
        # Fallback: scan for the bare integer near "answer is" / final line
        m = re.search(r"answer\s*(?:is|:)\s*(-?\d+)", output, re.IGNORECASE)
        if m:
            raw = m.group(1)
        else:
            # Last-resort: trailing integer on the final non-empty line
            lines = [l.strip() for l in output.splitlines() if l.strip()]
            if lines:
                m = re.search(r"(-?\d+)\s*$", lines[-1])
                if m:
                    raw = m.group(1)
    if raw is None:
        return False
    try:
        return int(raw.replace(",", "").strip()) == int(problem.answer)
    except ValueError:
        return False


def score_gsm8k(problem: Problem, output: str) -> bool:
    return _grade_integer(problem, output)


def score_aime2024(problem: Problem, output: str) -> bool:
    return _grade_integer(problem, output)


def score_needle(problem: Problem, output: str) -> bool:
    # Needle accepts either the #### convention OR the secret appearing
    # literally anywhere in the response (smaller models often answer
    # without following the format on long-context tasks).
    if _grade_integer(problem, output):
        return True
    return str(problem.answer) in output


# ── MMLU (multiple choice) ────────────────────────────────────────────────

_MMLU_LETTER = re.compile(r"\b([A-D])\b")


def score_mmlu(problem: Problem, output: str) -> bool:
    raw = _extract_answer(output)
    if raw:
        # Accept "A" or "(A)" or "A." or "Choice A"
        m = _MMLU_LETTER.search(raw.upper())
        if m:
            return m.group(1) == problem.answer
    # Fallback: last A/B/C/D on the final line
    lines = [l.strip() for l in output.splitlines() if l.strip()]
    if lines:
        m = _MMLU_LETTER.findall(lines[-1].upper())
        if m:
            return m[-1] == problem.answer
    return False


# ── HumanEval (executes candidate code in a subprocess sandbox) ──────────

# This is the only grader that runs untrusted output. The model emits a
# Python function body; we splice it back into the prompt's signature,
# write the result + the canonical test code to a temp file, and run it
# in a fresh subprocess with:
#   - 10 second wall-clock timeout
#   - stripped environment (no inherited LAI_* / API keys / HF_TOKEN)
#   - cwd = temp dir (so any file IO stays scoped)
#   - stdin closed
#   - separate temp file per problem so concurrent grades don't collide
# This is NOT a hardened security sandbox — a determined adversarial
# dataset commit could escape via subprocess / os.system. The mitigation
# is dataset provenance: HumanEval is vendored from openai/openai_humaneval
# and committed; a malicious update would be caught at PR review time.
_HUMANEVAL_TIMEOUT_SEC = 10.0


def score_humaneval(problem: Problem, output: str) -> bool:
    """Run the candidate completion against the canonical test code.

    The model output may be:
      (a) the function body only (continuing from the prompt)
      (b) a complete `def ...(): ...` block (it re-emitted the signature)
      (c) noise + an embedded `def ...(): ...` block somewhere

    We try in order: prompt+output verbatim, prompt+only-the-body part of
    output, only the def-block extracted from output. First one that
    passes the canonical tests wins."""
    candidates: list[str] = []
    candidates.append(problem.prompt + output)

    body = _extract_function_body(output)
    if body is not None:
        candidates.append(problem.prompt + body)

    full_def = _extract_def_block(output, problem.answer["entry_point"])
    if full_def:
        candidates.append(full_def)

    test_block = problem.answer["test"]
    entry_point = problem.answer["entry_point"]

    for cand in candidates:
        if _execute_humaneval(cand, test_block, entry_point):
            return True
    return False


def _extract_function_body(output: str) -> str | None:
    """If the model re-emitted the function signature, extract just the
    body so we can splice it after the prompt's signature. Heuristic —
    looks for the first `def ...:` line and returns everything after."""
    m = re.search(r"^(\s*)def\s+\w+\s*\([^)]*\)[^:]*:\s*\n", output, re.MULTILINE)
    if not m:
        return None
    return output[m.end():]


def _extract_def_block(output: str, entry_point: str) -> str | None:
    """Pull the full `def <entry_point>(...): ...` block from a noisy
    response (e.g. one wrapped in markdown ```python fences). Returns
    None if no such block found."""
    # Strip code fences if present
    fence = re.search(r"```(?:python)?\s*\n(.*?)\n```", output, re.DOTALL)
    text = fence.group(1) if fence else output
    # Find the def
    pat = re.compile(rf"^(def\s+{re.escape(entry_point)}\s*\(.*)", re.MULTILINE | re.DOTALL)
    m = pat.search(text)
    if not m:
        return None
    return text[m.start():]


def _execute_humaneval(code: str, test_block: str, entry_point: str) -> bool:
    """Write code+test to a temp file and run it. Returns True iff exit 0
    within the timeout. Stderr is captured but not surfaced (per-problem
    error inspection lives in the runner's debug log)."""
    runner = (
        code
        + "\n\n"
        + test_block
        + f"\n\ncheck({entry_point})\n"
    )
    with tempfile.TemporaryDirectory() as td:
        script_path = Path(td) / "candidate.py"
        script_path.write_text(runner, encoding="utf-8")
        # Stripped env: keep only PATH (Python interpreter discovery) +
        # SYSTEMROOT (Windows DLL loading). Everything else is dropped so
        # API keys / HF_TOKEN aren't visible to the candidate.
        env = {
            k: os.environ[k]
            for k in ("PATH", "SYSTEMROOT", "USERPROFILE", "TEMP", "TMP")
            if k in os.environ
        }
        try:
            proc = subprocess.run(
                [sys.executable, str(script_path)],
                cwd=td,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_HUMANEVAL_TIMEOUT_SEC,
                check=False,
            )
            return proc.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False


# ── Dispatch ──────────────────────────────────────────────────────────────

_GRADERS = {
    "humaneval": score_humaneval,
    "gsm8k": score_gsm8k,
    "aime2024": score_aime2024,
    "mmlu": score_mmlu,
    "needle": score_needle,
}


def score(problem: Problem, output: str) -> bool:
    grader = _GRADERS.get(problem.kind)
    if grader is None:
        raise ValueError(f"No grader registered for kind={problem.kind!r}")
    return grader(problem, output)
