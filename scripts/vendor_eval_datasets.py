"""Download and vendor the extended eval datasets:

  - MMLU-Pro (specialized knowledge, 14 domains, 10-choice)
  - BBH (Big Bench Hard, 23 sub-tasks, reasoning)
  - IFEval (instruction-following with machine-graders)
  - MATH (Hendrycks competition math, hard)
  - MBPP (basic Python problems, complement to HumanEval)
  - MT-Bench prompts (used for LLM-as-judge clarity scoring)

Output: parquet files under data/eval/datasets/<name>/.

Usage:
    python scripts/vendor_eval_datasets.py
    python scripts/vendor_eval_datasets.py --datasets mmlu_pro,ifeval
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
DATASETS_DIR = REPO / "data" / "eval" / "datasets"


def _save(rows: list[dict], name: str, file: str = None) -> Path:
    import pandas as pd
    out_dir = DATASETS_DIR / name
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / (file or f"{name}.parquet")
    df = pd.DataFrame(rows)
    df.to_parquet(out, index=False)
    print(f"  wrote {len(rows)} rows → {out}")
    return out


def vendor_mmlu_pro():
    """MMLU-Pro: 12K specialized-domain multiple-choice (10-way), harder than MMLU."""
    print("Vendoring MMLU-Pro…")
    from datasets import load_dataset
    ds = load_dataset("TIGER-Lab/MMLU-Pro", split="test")
    rows = []
    for r in ds:
        rows.append({
            "question": r["question"],
            "options": list(r["options"]),
            "answer": r["answer"],            # letter A-J
            "answer_index": r["answer_index"],
            "category": r["category"],
            "src": r.get("src", ""),
        })
    _save(rows, "mmlu_pro", "test.parquet")


def vendor_bbh():
    """BBH (Big Bench Hard): 23 challenging reasoning sub-tasks, 6.5K problems total."""
    print("Vendoring BBH…")
    from datasets import load_dataset
    # Maveriq/bigbenchhard has all 23 subtasks pre-aggregated under 'test'.
    rows = []
    for cfg in [
        "boolean_expressions", "causal_judgement", "date_understanding",
        "disambiguation_qa", "dyck_languages", "formal_fallacies",
        "geometric_shapes", "hyperbaton", "logical_deduction_five_objects",
        "logical_deduction_seven_objects", "logical_deduction_three_objects",
        "movie_recommendation", "multistep_arithmetic_two", "navigate",
        "object_counting", "penguins_in_a_table", "reasoning_about_colored_objects",
        "ruin_names", "salient_translation_error_detection", "snarks",
        "sports_understanding", "temporal_sequences",
        "tracking_shuffled_objects_five_objects",
        "tracking_shuffled_objects_seven_objects",
        "tracking_shuffled_objects_three_objects", "web_of_lies",
        "word_sorting",
    ]:
        try:
            ds = load_dataset("maveriq/bigbenchhard", cfg, split="train")
        except Exception as e:
            print(f"  skip {cfg}: {e}")
            continue
        for r in ds:
            rows.append({
                "subtask": cfg,
                "input": r["input"],
                "target": r["target"],
            })
    _save(rows, "bbh", "test.parquet")


def vendor_ifeval():
    """IFEval: 541 verifiable instructions (machine-checkable constraints)."""
    print("Vendoring IFEval…")
    from datasets import load_dataset
    ds = load_dataset("google/IFEval", split="train")
    rows = []
    for r in ds:
        rows.append({
            "key": r["key"],
            "prompt": r["prompt"],
            "instruction_id_list": list(r["instruction_id_list"]),
            "kwargs": list(r.get("kwargs", []) or []),
        })
    _save(rows, "ifeval", "test.parquet")


def vendor_math():
    """MATH: Hendrycks competition mathematics, levels 1–5. We keep levels 3–5
    (harder) — levels 1–2 overlap with GSM8K's range."""
    print("Vendoring MATH (levels 3–5)…")
    from datasets import load_dataset
    ds = load_dataset("HuggingFaceH4/MATH-500", split="test")
    rows = []
    for r in ds:
        if r.get("level", 0) < 3:
            continue
        rows.append({
            "problem": r["problem"],
            "level": r["level"],
            "subject": r["subject"],
            "answer": r["answer"],   # canonical numeric or LaTeX form
            "solution": r.get("solution", ""),
        })
    _save(rows, "math", "test.parquet")


def vendor_mbpp():
    """MBPP: 974 basic Python problems with test cases."""
    print("Vendoring MBPP…")
    from datasets import load_dataset
    ds = load_dataset("google-research-datasets/mbpp", "sanitized", split="test")
    rows = []
    for r in ds:
        rows.append({
            "task_id": r["task_id"],
            "prompt": r["prompt"],
            "code": r["code"],
            "test_list": list(r["test_list"]),
            "test_setup_code": r.get("test_setup_code", ""),
            "challenge_test_list": list(r.get("challenge_test_list", []) or []),
        })
    _save(rows, "mbpp", "test.parquet")


def vendor_mtbench():
    """MT-Bench: 80 multi-turn prompts used for LLM-as-judge quality scoring.
    The official source is lmsys/mt_bench (jsonl) — we keep only the first
    turn for the bench (single-turn LLM-as-judge is sufficient for clarity
    scoring). Falls back to direct HF download via huggingface_hub if the
    convenience loader is incompatible."""
    print("Vendoring MT-Bench prompts…")
    import json as _json
    import urllib.request
    # Direct download from the lm-sys repo — questions.jsonl is canonical
    url = "https://raw.githubusercontent.com/lm-sys/FastChat/main/fastchat/llm_judge/data/mt_bench/question.jsonl"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            raw = r.read().decode("utf-8")
    except Exception as exc:
        print(f"  fallback to HF: {exc}")
        from datasets import load_dataset
        ds = load_dataset("philschmid/mt-bench", split="train")
        rows = []
        for r in ds:
            turns = list(r.get("turns") or [])
            rows.append({
                "question_id": r.get("question_id", 0),
                "category": r.get("category", ""),
                "turn1": turns[0] if turns else "",
                "turn2": turns[1] if len(turns) > 1 else "",
                "reference1": "",
                "reference2": "",
            })
        _save(rows, "mtbench", "prompts.parquet")
        return
    rows = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        obj = _json.loads(line)
        turns = list(obj.get("turns") or [])
        ref = list(obj.get("reference") or [])
        rows.append({
            "question_id": obj.get("question_id", 0),
            "category": obj.get("category", ""),
            "turn1": turns[0] if turns else "",
            "turn2": turns[1] if len(turns) > 1 else "",
            "reference1": ref[0] if ref else "",
            "reference2": ref[1] if len(ref) > 1 else "",
        })
    _save(rows, "mtbench", "prompts.parquet")


REGISTRY = {
    "mmlu_pro": vendor_mmlu_pro,
    "bbh": vendor_bbh,
    "ifeval": vendor_ifeval,
    "math": vendor_math,
    "mbpp": vendor_mbpp,
    "mtbench": vendor_mtbench,
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--datasets", default="all",
                   help="comma-separated subset, or 'all' (default)")
    args = p.parse_args()

    DATASETS_DIR.mkdir(parents=True, exist_ok=True)

    if args.datasets == "all":
        targets = list(REGISTRY)
    else:
        targets = [t.strip() for t in args.datasets.split(",") if t.strip()]
        unknown = [t for t in targets if t not in REGISTRY]
        if unknown:
            print(f"unknown datasets: {unknown}; known: {list(REGISTRY)}",
                  file=sys.stderr)
            return 2

    for name in targets:
        try:
            REGISTRY[name]()
        except Exception as exc:
            print(f"FAIL {name}: {exc}", file=sys.stderr)
            import traceback; traceback.print_exc()
    return 0


if __name__ == "__main__":
    sys.exit(main())
