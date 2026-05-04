"""Capability bench across one or more tiers.

Usage:
    # Quick smoke test — one tier, one capability, fast depth (~30 problems)
    python scripts/eval_tiers.py --tiers fast --capabilities coding --depth fast

    # All capabilities on every chat tier, fast depth — ~3-4 hours total
    python scripts/eval_tiers.py --tiers all --capabilities all --depth fast

    # Overnight definitive run — all caps × all tiers × full depth
    python scripts/eval_tiers.py \\
        --tiers all --capabilities all --depth full \\
        --deadline-utc 2026-05-04T19:00:00Z \\
        --out data/eval/results/full-overnight-20260504.json

Capabilities (vendored datasets, no internet needed):
    reasoning    AIME 2024 (30 problems)
    math         GSM8K (1319 problems)
    coding       HumanEval (164 problems, executes candidate Python in subprocess)
    knowledge    MMLU subset (399 problems, 57 subjects, stratified ~7/subject)
    long_context Needle in haystack (4-16 problems at 4k/16k/65k/131k ctx)

Depth selectors (per-dataset sample sizes):
    fast    30/50/15/50/4   — ~30 min/tier across all capabilities at 25 tok/s
    medium  80/200/30/150/8 — ~2 hr/tier
    full    164/1319/30/399/16 — ~6-8 hr/tier (the overnight run)

Tier discovery: by default every chat tier exposed by the live backend
is benched. Use --tiers to restrict. The runner discovers the backend
port via data/runtime/backend.json (written by observability.install)
or falls back to --api.

Output: a JSON snapshot under data/eval/results/ + a markdown summary
under data/eval/results/. Both go to stdout if --out is omitted.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make the project importable whether the script is run as
# `python scripts/eval_tiers.py` or `python -m scripts.eval_tiers`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _resolve_api(arg_api: str | None) -> str:
    """Find the live backend. Order:
      1. --api flag
      2. LAI_API_BASE env var
      3. data/runtime/backend.json (written by observability)
      4. http://127.0.0.1:18000 (the launcher's fixed default)
    """
    if arg_api:
        return arg_api
    env_api = os.getenv("LAI_API_BASE")
    if env_api:
        return env_api
    try:
        from backend import observability as obs
        for snap in obs.state_snapshot():
            if snap.get("component") == "backend" and snap.get("alive") and snap.get("port"):
                return f"http://127.0.0.1:{snap['port']}"
    except Exception:  # noqa: BLE001
        pass
    return "http://127.0.0.1:18000"


def _list_chat_tiers(api_base: str) -> list[str]:
    """Read /v1/models, filter to tier.* virtual models. The "tier."
    prefix is what the backend uses to route chat requests through the
    VRAMScheduler instead of straight to a single llama-server."""
    import json
    import urllib.request
    req = urllib.request.Request(api_base.rstrip("/") + "/v1/models")
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.load(r)
    return sorted(
        m["id"].removeprefix("tier.")
        for m in data.get("data", [])
        if isinstance(m.get("id"), str) and m["id"].startswith("tier.")
    )


def _parse_deadline(s: str | None) -> float | None:
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def main() -> int:
    # Observability + per-run log file. Pulled in lazily because the
    # script's argparse error path should fire even if observability
    # imports fail (e.g. setproctitle missing in a fresh venv).
    try:
        from backend import observability as obs
        obs.install("eval")
    except Exception as exc:  # noqa: BLE001
        print(f"warn: observability.install failed: {exc}", file=sys.stderr)

    p = argparse.ArgumentParser(
        prog="eval_tiers",
        description="Capability bench across one or more tiers (vendored datasets, offline).",
    )
    p.add_argument("--api", default=None,
                   help="Backend API base URL. Default: discover via data/runtime/backend.json.")
    p.add_argument("--tiers", default="all",
                   help="Comma-separated tier names, or 'all' (default).")
    p.add_argument("--capabilities", default="all",
                   help="Comma-separated capability names, or 'all'. "
                        "Choices: reasoning, math, coding, knowledge, long_context.")
    p.add_argument("--depth", choices=("fast", "medium", "full"), default="fast",
                   help="Per-dataset sample size. fast (~30 min/tier), medium (~2 hr), full (overnight).")
    p.add_argument("--max-tokens", type=int, default=2048,
                   help="Per-problem max output tokens. Reasoning capabilities may need bumping to 8192.")
    p.add_argument("--per-problem-timeout", type=int, default=300,
                   help="Per-problem wall-clock timeout in seconds (default 5 min).")
    p.add_argument("--think", choices=("auto", "on", "off"), default="auto",
                   help="Thinking mode: auto (capability-default), on (force), off (force).")
    p.add_argument("--deadline-utc", default=None,
                   help="Overall wall-clock deadline as RFC3339 UTC. Problems past this skip with reason='deadline'.")
    p.add_argument("--out", default=None,
                   help="Output JSON path. Default: data/eval/results/eval-<timestamp>.json.")
    args = p.parse_args()

    from backend.eval.runner import CAPABILITIES, run_cell, write_json, write_markdown

    api = _resolve_api(args.api)

    # Capability list
    if args.capabilities == "all":
        capabilities = list(CAPABILITIES.keys())
    else:
        capabilities = [c.strip() for c in args.capabilities.split(",") if c.strip()]
        unknown = [c for c in capabilities if c not in CAPABILITIES]
        if unknown:
            print(f"Unknown capability/ies: {unknown}. "
                  f"Known: {sorted(CAPABILITIES.keys())}", file=sys.stderr)
            return 2

    # Tier list
    try:
        available_tiers = _list_chat_tiers(api)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to discover tiers from {api}: {exc}", file=sys.stderr)
        return 3
    if args.tiers == "all":
        tiers = available_tiers
    else:
        tiers = [t.strip() for t in args.tiers.split(",") if t.strip()]
        unknown = [t for t in tiers if t not in available_tiers]
        if unknown:
            print(f"Unknown tier(s): {unknown}. Available: {available_tiers}",
                  file=sys.stderr)
            return 4

    deadline = _parse_deadline(args.deadline_utc)
    think_arg = {"auto": None, "on": True, "off": False}[args.think]

    print(f"Eval plan: {len(tiers)} tier(s) × {len(capabilities)} capability/ies × depth={args.depth}")
    print(f"  api={api}")
    print(f"  tiers={tiers}")
    print(f"  capabilities={capabilities}")
    if deadline:
        print(f"  deadline={datetime.fromtimestamp(deadline):%Y-%m-%d %H:%M %Z}")
    print()

    results = []
    eval_started = time.time()
    for tier in tiers:
        for cap in capabilities:
            if deadline and time.time() > deadline:
                print(f"  Skip {tier}/{cap}: deadline already past.")
                continue
            print(f"  Running {tier} × {cap}...")
            cell = run_cell(
                api, tier, cap, args.depth,
                max_tokens=args.max_tokens,
                per_problem_timeout=args.per_problem_timeout,
                think=think_arg,
                deadline=deadline,
            )
            print(f"    -> pass {cell.n_passed}/{cell.n_problems} = {cell.pass_rate*100:.1f}% "
                  f"in {cell.wall_seconds:.0f}s (mean lat {cell.mean_latency_s:.1f}s)")
            results.append(cell)
    eval_finished = time.time()

    # Snapshot.
    if args.out:
        out_path = Path(args.out)
    else:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = (
            Path(__file__).resolve().parent.parent
            / "data" / "eval" / "results" / f"eval-{ts}.json"
        )
    write_json(results, out_path)
    md_path = out_path.with_suffix(".md")
    write_markdown(results, md_path)

    print()
    print(f"Total wall: {(eval_finished - eval_started) / 60:.1f} min")
    print(f"JSON:     {out_path}")
    print(f"Markdown: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
