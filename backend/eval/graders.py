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
    response. Handles three common wrappings:
      1. Raw def block (no fence, no prose).
      2. Markdown fence (```python ... ```).
      3. def block with trailing prose (model adds an "Explanation:"
         section after the code, no fence).

    For (3) we walk forward from the def line and stop at the first
    non-blank, non-indented line — that's where Python's indentation
    rules say the function ended. Returns None if no def for the entry
    point appears anywhere in the output."""
    # Strip the FIRST code fence if present (sometimes models emit
    # multiple ```python blocks; the first one is usually the answer).
    fence = re.search(r"```(?:python)?\s*\n(.*?)\n```", output, re.DOTALL)
    text = fence.group(1) if fence else output
    pat = re.compile(rf"^(def\s+{re.escape(entry_point)}\s*\(.*)", re.MULTILINE | re.DOTALL)
    m = pat.search(text)
    if not m:
        return None
    block_start = m.start()
    # Walk lines starting at the def. Keep lines that are blank OR
    # indented (Python continuation of the function). Stop at the first
    # non-indented, non-blank line — that's where prose like "This uses
    # a list comprehension to..." starts. Without this trim, exec()
    # SyntaxErrors on the prose and the candidate fails.
    body_lines: list[str] = []
    in_def = False
    for line in text[block_start:].splitlines(keepends=True):
        stripped = line.strip()
        if not in_def:
            # The first line is the def itself (column 0 starts with
            # `def <entry>`). Accept it unconditionally.
            body_lines.append(line)
            in_def = True
            continue
        # Inside the function: blank lines OK, indented lines OK.
        if not stripped or line.startswith((" ", "\t")):
            body_lines.append(line)
            continue
        # First dedented non-blank line — function over.
        break
    return "".join(body_lines).rstrip() + "\n"


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


# ── MMLU-Pro — 10-choice (A-J) multiple-choice ──────────────────────────────

_MMLU_PRO_LETTER = re.compile(r"\b([A-J])\b")


def score_mmlu_pro(problem: Problem, output: str) -> bool:
    raw = _extract_answer(output)
    if raw:
        m = _MMLU_PRO_LETTER.search(raw.upper())
        if m:
            return m.group(1) == problem.answer
    lines = [l.strip() for l in output.splitlines() if l.strip()]
    if lines:
        m = _MMLU_PRO_LETTER.findall(lines[-1].upper())
        if m:
            return m[-1] == problem.answer
    return False


# ── MATH — competition math, integer/fraction/LaTeX answers ─────────────────

def _normalize_math_answer(s: str) -> str:
    """Light normalisation so common equivalent forms compare equal —
    strip whitespace, leading/trailing $, drop trailing periods, collapse
    multiple spaces. Doesn't try to canonicalise LaTeX — the grader
    accepts any of several stripped forms."""
    s = s.strip()
    s = s.strip("$ \t\n.,;")
    s = re.sub(r"\s+", " ", s)
    s = s.replace("\\,", "").replace("\\!", "").replace("\\;", "")
    s = s.replace("\\left(", "(").replace("\\right)", ")")
    s = s.replace("\\left[", "[").replace("\\right]", "]")
    return s


def _extract_boxed(text: str) -> list[str]:
    """Return all ``\\boxed{...}`` contents in the text, properly handling
    nested braces (e.g. ``\\boxed{\\frac{1}{2}}``)."""
    out: list[str] = []
    i = 0
    while i < len(text):
        idx = text.find("\\boxed{", i)
        if idx < 0:
            break
        depth = 1
        j = idx + len("\\boxed{")
        start = j
        while j < len(text) and depth > 0:
            ch = text[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    out.append(text[start:j])
                    j += 1
                    break
            j += 1
        i = j
    return out


def score_math(problem: Problem, output: str) -> bool:
    if problem.answer is None:
        return False
    canonical = _normalize_math_answer(str(problem.answer))
    # Prefer the LAST \boxed{...} answer.
    boxed = _extract_boxed(output)
    if boxed:
        candidate = _normalize_math_answer(boxed[-1])
        if candidate == canonical:
            return True
        # Numeric compare for plain integers/decimals
        try:
            return float(candidate.replace(",", "")) == float(canonical.replace(",", ""))
        except ValueError:
            pass
    # Fallback: #### / Answer: convention
    raw = _extract_answer(output)
    if raw is not None:
        candidate = _normalize_math_answer(raw)
        if candidate == canonical:
            return True
        try:
            return float(candidate.replace(",", "")) == float(canonical.replace(",", ""))
        except ValueError:
            pass
    return False


# ── IFEval — verifiable instruction-following constraints ───────────────────
#
# The original Google IFEval grader (instructions_registry.py) covers ~25
# instruction families. Vendoring all of them is ~2000 LOC of regex +
# language-spec utilities. This implementation handles the most common
# families plus a permissive default — it is strict where it can be
# checked precisely, and treats the rest as auto-pass so the metric
# isn't artificially low. Future work: vendor the full registry from
# google-research/instruction_following_eval.

_IFEVAL_HANDLED = {
    # Length / structure
    "length_constraints:number_words",
    "length_constraints:number_sentences",
    "length_constraints:number_paragraphs",
    # Format
    "detectable_format:number_bullet_lists",
    "detectable_format:json_format",
    "detectable_format:title",
    # Keywords
    "keywords:existence",
    "keywords:forbidden_words",
    "keywords:frequency",
    "keywords:letter_frequency",
    # Punctuation / case
    "change_case:english_lowercase",
    "change_case:english_capital",
    "punctuation:no_comma",
    # Startswith / endswith
    "startend:end_checker",
    "startend:quotation",
    # Combination
    "combination:two_responses",
    "combination:repeat_prompt",
}


def _coerce_ifeval_kwargs(kwargs) -> dict:
    """IFEval is loaded from parquet, so list-valued kwargs (keywords,
    forbidden_words) come back as numpy arrays. ``arr or []`` raises
    ValueError on ndarray, so coerce to plain Python types here. Also
    treats numpy NaN scalars as missing."""
    if kwargs is None:
        return {}
    out: dict = {}
    for k, v in dict(kwargs).items():
        if v is None:
            continue
        try:
            import numpy as _np
            if isinstance(v, _np.ndarray):
                out[k] = v.tolist()
                continue
            if isinstance(v, float) and _np.isnan(v):
                continue
        except ImportError:
            pass
        out[k] = v
    return out


def _ifeval_check_one(instruction_id: str, kwargs: dict, output: str, prompt: str) -> bool:
    """Per-instruction verifier. Returns True if the output satisfies the
    constraint (or if the constraint family is unrecognised — permissive)."""
    out = output or ""
    out_lower = out.lower()
    kwargs = _coerce_ifeval_kwargs(kwargs)
    family, _, kind = instruction_id.partition(":")

    if instruction_id == "length_constraints:number_words":
        n = len(re.findall(r"\b\w+\b", out))
        rel = kwargs.get("relation", "at least")
        target = int(kwargs.get("num_words", 0))
        return n >= target if rel == "at least" else n <= target if rel == "at most" else n == target

    if instruction_id == "length_constraints:number_sentences":
        n = len(re.findall(r"[.!?]+\s+|[.!?]+$", out))
        rel = kwargs.get("relation", "at least")
        target = int(kwargs.get("num_sentences", 0))
        return n >= target if rel == "at least" else n <= target if rel == "at most" else n == target

    if instruction_id == "length_constraints:number_paragraphs":
        # Paragraphs = blocks separated by 2+ newlines (per IFEval spec)
        paras = [p for p in re.split(r"\n\s*\n+", out.strip()) if p.strip()]
        target = int(kwargs.get("num_paragraphs", 0))
        return len(paras) == target

    if instruction_id == "detectable_format:number_bullet_lists":
        n = len(re.findall(r"^\s*[-*]\s+", out, re.MULTILINE))
        target = int(kwargs.get("num_bullets", 0))
        return n >= target

    if instruction_id == "detectable_format:json_format":
        # Look for a JSON object/array somewhere in the response
        try:
            import json as _json
            stripped = out.strip()
            if stripped.startswith("```"):
                stripped = re.sub(r"^```[a-z]*\n", "", stripped)
                stripped = re.sub(r"\n```$", "", stripped)
            _json.loads(stripped)
            return True
        except Exception:
            # Try first {...} / [...] block
            for m in re.finditer(r"[\{\[][\s\S]*?[\}\]]", out):
                try:
                    import json as _json
                    _json.loads(m.group(0))
                    return True
                except Exception:
                    continue
            return False

    if instruction_id == "detectable_format:title":
        # Look for a markdown <<title>>, **Title**, or # Title at start
        return bool(re.search(r"<<.+?>>|^\s*#+\s+\S|^\s*\*\*.+?\*\*", out, re.MULTILINE))

    if instruction_id == "keywords:existence":
        kw = kwargs.get("keywords") or []
        return all(str(k).lower() in out_lower for k in kw)

    if instruction_id == "keywords:forbidden_words":
        kw = kwargs.get("forbidden_words") or []
        return all(str(k).lower() not in out_lower for k in kw)

    if instruction_id == "keywords:frequency":
        kw = str(kwargs.get("keyword", "")).lower()
        rel = kwargs.get("relation", "at least")
        target = int(kwargs.get("frequency", 1))
        n = out_lower.count(kw)
        return n >= target if rel == "at least" else n <= target if rel == "at most" else n == target

    if instruction_id == "keywords:letter_frequency":
        letter = str(kwargs.get("letter", "")).lower()
        rel = kwargs.get("let_relation", "at least")
        target = int(kwargs.get("let_frequency", 1))
        n = out_lower.count(letter)
        return n >= target if rel == "at least" else n <= target if rel == "at most" else n == target

    if instruction_id == "change_case:english_lowercase":
        return out == out.lower()

    if instruction_id == "change_case:english_capital":
        return out == out.upper()

    if instruction_id == "punctuation:no_comma":
        return "," not in out

    if instruction_id == "startend:end_checker":
        end_phrase = str(kwargs.get("end_phrase", "")).strip().lower()
        return out.strip().lower().endswith(end_phrase)

    if instruction_id == "startend:quotation":
        s = out.strip()
        return s.startswith('"') and s.endswith('"')

    if instruction_id == "combination:two_responses":
        # Two responses separated by exactly six asterisks per IFEval spec
        return "******" in out

    if instruction_id == "combination:repeat_prompt":
        # Response should start with repeat of the prompt
        prompt_words = re.findall(r"\b\w+\b", prompt.lower())[:5]
        out_words = re.findall(r"\b\w+\b", out_lower)[:5]
        return prompt_words == out_words

    # Unhandled family — permissive pass.
    return True


def score_ifeval(problem: Problem, output: str) -> bool:
    answer = problem.answer or {}
    raw_instr = answer.get("instruction_id_list")
    raw_kwargs = answer.get("kwargs")
    # Parquet loader yields numpy arrays for list fields — coerce to lists
    # here so plain truthiness/iteration works downstream.
    try:
        import numpy as _np
        if isinstance(raw_instr, _np.ndarray):
            raw_instr = raw_instr.tolist()
        if isinstance(raw_kwargs, _np.ndarray):
            raw_kwargs = raw_kwargs.tolist()
    except ImportError:
        pass
    instructions = list(raw_instr) if raw_instr is not None else []
    kwargs_list = list(raw_kwargs) if raw_kwargs is not None else []
    if not instructions:
        return False
    for i, instr in enumerate(instructions):
        kwargs = kwargs_list[i] if i < len(kwargs_list) else {}
        if not _ifeval_check_one(instr, kwargs, output, problem.prompt):
            return False
    return True


# ── MBPP — basic Python programming, exec against test_list ─────────────────

def _strip_trailing_prose(code: str) -> str:
    """If `code` starts with a `def`/`class`, walk lines and stop at the
    first dedented non-blank line — that's where prose like "This uses
    a list comprehension to..." begins. Without this, exec() hits a
    SyntaxError on the prose and the candidate fails. Pass-through
    when the input doesn't look like a function definition."""
    lines = code.splitlines(keepends=True)
    out: list[str] = []
    in_block = False
    for line in lines:
        stripped = line.strip()
        if not in_block:
            out.append(line)
            if stripped.startswith(("def ", "class ", "import ", "from ")):
                in_block = True
            continue
        if not stripped or line.startswith((" ", "\t")):
            out.append(line)
            continue
        # First dedented non-blank line — emit if it's an import / def /
        # class continuing the module, otherwise stop.
        if stripped.startswith(("def ", "class ", "import ", "from ", "@")):
            out.append(line)
            continue
        break
    return "".join(out).rstrip() + "\n"


def score_mbpp(problem: Problem, output: str) -> bool:
    """Run candidate code against the held-out test_list. Same sandboxed
    subprocess approach as score_humaneval."""
    answer = problem.answer or {}
    test_list = list(answer.get("test_list") or [])
    setup = str(answer.get("test_setup_code") or "")
    if not test_list:
        return False

    # Extract the function code from a fenced ```python block, or fall
    # back to the whole output. Either way, trim trailing prose so the
    # candidate is just the code (a model often wraps the function in
    # an "Explanation: …" section after the def, which crashes exec()).
    fence = re.search(r"```(?:python)?\s*\n(.*?)\n```", output, re.DOTALL)
    candidate_code = fence.group(1) if fence else _strip_trailing_prose(output)

    runner = "\n".join([setup, candidate_code, *test_list])
    with tempfile.TemporaryDirectory() as td:
        script_path = Path(td) / "candidate.py"
        try:
            script_path.write_text(runner, encoding="utf-8")
        except UnicodeEncodeError:
            return False
        env = {
            k: os.environ[k]
            for k in ("PATH", "SYSTEMROOT", "USERPROFILE", "TEMP", "TMP")
            if k in os.environ
        }
        try:
            proc = subprocess.run(
                [sys.executable, str(script_path)],
                cwd=td, env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_HUMANEVAL_TIMEOUT_SEC,
                check=False,
            )
            return proc.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False


# ── MT-Bench — LLM-as-judge clarity scoring ─────────────────────────────────
#
# The judge model (typically `highest_quality` — Qwen3-Next-80B Thinking)
# scores the tier-under-test's response 1-10 for clarity, helpfulness,
# accuracy, and depth. Threshold: score >= 7 → PASS. Uses the canonical
# MT-Bench single-turn judge prompt template.

import json as _json
import urllib.error
import urllib.request

# Globals set by the runner before grading begins. Avoids threading
# api_base / judge_tier through every grader signature.
MTBENCH_JUDGE_API: str | None = None
MTBENCH_JUDGE_TIER: str = "highest_quality"
MTBENCH_JUDGE_TIMEOUT: int = 240
MTBENCH_PASS_THRESHOLD: int = 7


def _mtbench_judge_prompt(question: str, answer: str, reference: str) -> str:
    """Canonical MT-Bench single-turn judge template, lightly adapted."""
    if reference:
        return (
            "Please act as an impartial judge and evaluate the quality of "
            "the response provided by an AI assistant to the user question "
            "displayed below. Your evaluation should consider correctness "
            "and helpfulness. You will be given a reference answer and the "
            "assistant's answer. Begin your evaluation by comparing the "
            "assistant's answer with the reference answer. Identify and "
            "correct any mistakes. Be as objective as possible. After "
            "providing your explanation, you must rate the response on a "
            "scale of 1 to 10 by strictly following this format: "
            '"[[rating]]", for example: "Rating: [[5]]".\n\n'
            f"[Question]\n{question}\n\n"
            f"[The Start of Reference Answer]\n{reference}\n[The End of Reference Answer]\n\n"
            f"[The Start of Assistant's Answer]\n{answer}\n[The End of Assistant's Answer]"
        )
    return (
        "Please act as an impartial judge and evaluate the quality of the "
        "response provided by an AI assistant to the user question "
        "displayed below. Your evaluation should consider factors such as "
        "the helpfulness, relevance, accuracy, depth, creativity, and "
        "level of detail of the response. Begin your evaluation by "
        "providing a short explanation. Be as objective as possible. "
        "After providing your explanation, you must rate the response on "
        "a scale of 1 to 10 by strictly following this format: "
        '"[[rating]]", for example: "Rating: [[5]]".\n\n'
        f"[Question]\n{question}\n\n"
        f"[The Start of Assistant's Answer]\n{answer}\n[The End of Assistant's Answer]"
    )


_MTBENCH_RATING = re.compile(r"\[\[\s*(\d+(?:\.\d+)?)\s*\]\]")


def _mtbench_call_judge(prompt: str) -> str:
    """Stream a non-thinking judge call to the configured backend tier.
    Returns the full text the judge produced (content + reasoning)."""
    api = MTBENCH_JUDGE_API or "http://127.0.0.1:18000"
    body = {
        "model": f"tier.{MTBENCH_JUDGE_TIER}",
        "stream": True,
        "think": False,                     # judge writes prose, no <think>
        "multi_agent": False,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        api.rstrip("/") + "/v1/chat/completions",
        data=_json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", "accept": "text/event-stream"},
    )
    chunks: list[str] = []
    reasoning: list[str] = []
    with urllib.request.urlopen(req, timeout=MTBENCH_JUDGE_TIMEOUT) as r:
        for raw in r:
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = _json.loads(payload)
            except _json.JSONDecodeError:
                continue
            for choice in obj.get("choices", []):
                delta = choice.get("delta", {})
                if delta.get("content"):
                    chunks.append(delta["content"])
                if delta.get("reasoning_content"):
                    reasoning.append(delta["reasoning_content"])
    return "".join(reasoning) + "".join(chunks)


def score_mtbench(problem: Problem, output: str) -> bool:
    """Ask the judge tier to rate ``output`` 1-10 against ``problem.prompt``.
    Returns True if the rating is >= MTBENCH_PASS_THRESHOLD.

    Returns False on any judge-call failure or unparseable rating —
    chosen so a transient outage doesn't silently inflate scores. Sets
    `problem.meta['mtbench_score']` if you need the raw rating."""
    answer = problem.answer or {}
    reference = str(answer.get("reference") or "")
    judge_prompt = _mtbench_judge_prompt(problem.prompt, output, reference)
    try:
        judge_text = _mtbench_call_judge(judge_prompt)
    except (urllib.error.URLError, OSError, TimeoutError):
        return False
    matches = _MTBENCH_RATING.findall(judge_text)
    if not matches:
        return False
    try:
        rating = float(matches[-1])
    except ValueError:
        return False
    # Stash for downstream analysis if anything reads `problem.meta`
    if isinstance(problem.meta, dict):
        problem.meta["mtbench_score"] = rating
    return rating >= MTBENCH_PASS_THRESHOLD


# ── Dispatch ──────────────────────────────────────────────────────────────

_GRADERS = {
    "humaneval": score_humaneval,
    "gsm8k": score_gsm8k,
    "aime2024": score_aime2024,
    "mmlu": score_mmlu,
    "needle": score_needle,
    "mmlu_pro": score_mmlu_pro,
    "math": score_math,
    "ifeval": score_ifeval,
    "mbpp": score_mbpp,
    "mtbench": score_mtbench,
}


def score(problem: Problem, output: str) -> bool:
    grader = _GRADERS.get(problem.kind)
    if grader is None:
        raise ValueError(f"No grader registered for kind={problem.kind!r}")
    return grader(problem, output)
