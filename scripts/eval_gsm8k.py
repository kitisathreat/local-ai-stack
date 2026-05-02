"""Tiny GSM8K runner against the local /v1/chat/completions endpoint.

A trimmed-down stand-in for lm-eval-harness while the backend doesn't
yet support non-streaming responses (lm-eval's openai-chat-completions
backend errors on SSE). Produces a single accuracy number per run so
quality changes (new quants, embedder swaps, model upgrades) get a
comparable baseline.

Usage:
    python scripts/eval_gsm8k.py --tier fast --limit 50
    python scripts/eval_gsm8k.py --tier versatile --limit 100 --out data/eval/v.json

The dataset is downloaded from HF (gsm8k, "main" split, "test" rows).
Scoring: extract the final number from the model response and compare
to the gold answer. Loose match (decimal-tolerant) — same scoring rule
as the original GSM8K paper.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import urllib.request
from pathlib import Path


GSM8K_PROMPT_PREFIX = (
    "Solve the math problem step by step. After your reasoning, end your "
    "answer with the line `Answer: <number>` where <number> is the final "
    "numeric answer.\n\nProblem: "
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--api", default="http://127.0.0.1:18000",
                   help="Backend base URL")
    p.add_argument("--tier", default="fast",
                   help="Tier name (sent as model=tier.<name>)")
    p.add_argument("--limit", type=int, default=50,
                   help="How many GSM8K test rows to run (default 50)")
    p.add_argument("--out", default="",
                   help="Path to dump full result JSON (default: data/eval/<tier>-<ts>.json)")
    return p.parse_args()


def load_gsm8k(limit: int) -> list[dict]:
    """Pull GSM8K test rows directly from the canonical HF parquet file.
    Cached after first fetch in data/eval/gsm8k_test.jsonl so repeat
    runs don't hit the network. Requires `pyarrow` for the parquet
    decode (already in venv-backend / venv-jupyter)."""
    cache = Path("data/eval/gsm8k_test.jsonl")
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        url = "https://huggingface.co/datasets/gsm8k/resolve/main/main/test-00000-of-00001.parquet"
        req = urllib.request.Request(url, headers={"User-Agent": "lai-eval/1.0"})
        tmp = cache.with_suffix(".parquet")
        with urllib.request.urlopen(req, timeout=120) as r, tmp.open("wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
        import pyarrow.parquet as pq  # type: ignore
        table = pq.read_table(str(tmp))
        with cache.open("w", encoding="utf-8") as out:
            for row in table.to_pylist():
                out.write(json.dumps(row) + "\n")
        tmp.unlink()
    rows: list[dict] = []
    with cache.open(encoding="utf-8") as f:
        for line in f:
            rows.append(json.loads(line))
            if len(rows) >= limit:
                break
    return rows


def gold_answer(answer_field: str) -> str:
    """GSM8K marks the final answer as `#### N` at the end of the rationale."""
    m = re.search(r"####\s*([\-0-9.,]+)", answer_field)
    return m.group(1).replace(",", "").strip() if m else ""


_NUM_PAT = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def model_answer(text: str) -> str:
    """Pick the predicted number. Prefer an `Answer: N` line; else last
    number in the text. Strips commas and trailing periods."""
    m = re.search(r"(?im)^\s*answer\s*[:=]\s*([\-0-9.,]+)", text)
    if m:
        return m.group(1).replace(",", "").rstrip(".")
    nums = _NUM_PAT.findall(text)
    return nums[-1].replace(",", "").rstrip(".") if nums else ""


def numbers_equal(a: str, b: str) -> bool:
    if not a or not b:
        return False
    try:
        return abs(float(a) - float(b)) < 1e-6
    except ValueError:
        return a.strip() == b.strip()


def run_one(api_base: str, tier: str, question: str, timeout: int = 120) -> tuple[str, float]:
    """Send the prompt, parse the SSE stream, return (assembled_text, wall_seconds)."""
    body = json.dumps({
        "model": f"tier.{tier}",
        "messages": [{"role": "user", "content": GSM8K_PROMPT_PREFIX + question}],
    }).encode("utf-8")
    req = urllib.request.Request(
        api_base.rstrip("/") + "/v1/chat/completions",
        data=body,
        headers={"content-type": "application/json", "accept": "text/event-stream"},
    )
    started = time.time()
    chunks: list[str] = []
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", errors="ignore").rstrip("\n")
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            for ch in obj.get("choices") or []:
                t = (ch.get("delta") or {}).get("content")
                if isinstance(t, str):
                    chunks.append(t)
    return "".join(chunks), time.time() - started


def main() -> int:
    args = parse_args()
    rows = load_gsm8k(args.limit)
    if not rows:
        print("Failed to fetch GSM8K rows.", file=sys.stderr)
        return 2

    print(f"Running {len(rows)} GSM8K test rows on tier.{args.tier} via {args.api}")
    correct = 0
    latencies: list[float] = []
    detail: list[dict] = []
    for i, row in enumerate(rows, 1):
        q = row["question"]
        gold = gold_answer(row["answer"])
        try:
            text, dt = run_one(args.api, args.tier, q)
        except Exception as e:
            print(f"  [{i:>3}/{len(rows)}] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
            detail.append({"i": i, "gold": gold, "pred": "", "ok": False, "error": str(e)})
            continue
        pred = model_answer(text)
        ok = numbers_equal(pred, gold)
        correct += int(ok)
        latencies.append(dt)
        flag = "OK " if ok else "X  "
        print(f"  [{i:>3}/{len(rows)}] {flag} pred={pred!r:<10} gold={gold!r:<10} {dt:5.1f}s")
        detail.append({"i": i, "gold": gold, "pred": pred, "ok": ok, "latency_s": dt})

    n = len(detail)
    acc = correct / n if n else 0.0
    print()
    print(f"Accuracy: {correct}/{n} = {acc:.1%}")
    if latencies:
        print(f"Latency:  median {statistics.median(latencies):.1f}s, "
              f"mean {statistics.mean(latencies):.1f}s, "
              f"p90 {sorted(latencies)[int(0.9*len(latencies))]:.1f}s")

    out = args.out or f"data/eval/{args.tier}-gsm8k-{int(time.time())}.json"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps({
        "tier": args.tier,
        "task": "gsm8k_cot",
        "n": n,
        "correct": correct,
        "accuracy": acc,
        "detail": detail,
    }, indent=2), encoding="utf-8")
    print(f"Saved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
