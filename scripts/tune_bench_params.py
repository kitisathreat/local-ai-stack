"""Per-(tier, think_mode) sampling-parameter auto-tuner with composite
cross-category scoring.

Goal: discover ONE overlay per (tier, think_mode) that maximises mean
performance across knowledge, math, coding, and tool_use simultaneously
— not per-capability. The same overlay then applies to every cell of
that tier in subsequent benches.

Algorithm
---------
For each (tier, think_mode):
  1. Build a candidate grid (Cartesian product of TUNABLE_PARAMS, sampled
     down to ``--max-candidates`` via ``param_tuning.propose_grid``).
  2. For each candidate overlay:
       a. Run ``--n-per-category`` problems on each category's
          representative capability (knowledge → MMLU, math → GSM8K,
          coding → HumanEval, tool_use → IFEval). 4 categories × N.
       b. Composite score = arithmetic mean of per-category Wilson-LCB.
          (Switch to weighted mean by setting ``--category-weights``.)
  3. Pick the candidate with the highest composite Wilson-LCB. Persist
     to data/eval/tuned_params.json keyed as ``<tier>/think_<off|on>``.

Why composite of Wilson-LCB instead of mean rate
------------------------------------------------
At the small N values per category we use during tuning, the rate point
estimate is noisy. Wilson-LCB penalises high-variance candidates, so a
candidate at 12/20 = 60% with LCB ≈ 0.39 doesn't beat 16/30 = 53% with
LCB ≈ 0.36 by accident. Averaging LCBs across categories gives a
"defensible-everywhere" overlay rather than one that's spectacularly
good at one task and terrible at another.

Usage
-----
    python scripts/tune_bench_params.py \\
        --tiers swarm,fast,versatile,coding,highest_quality \\
        --think off,on \\
        --categories knowledge,math,coding,tool_use \\
        --n-per-category 30 \\
        --max-candidates 6
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.eval import param_tuning as _pt
from backend.eval.datasets import Depth
from backend.eval.runner import run_cell


def _evict_other_llama_servers() -> None:
    """Kill llama-server.exe processes that are NOT serving the embedding
    (port 8090) or reranker (port 8091) tiers. The residency manager
    sometimes wedges between tier swaps, leaving an inert llama-server
    that holds VRAM without serving traffic. We don't know which tier
    each surviving process serves, so the safe rule is: kill anything
    that isn't pinned (embedding/reranker), let the backend respawn the
    target tier on demand."""
    import subprocess as _sp, re as _re
    try:
        out = _sp.check_output(
            ["pwsh", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"name='llama-server.exe'\" | "
             "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress -Depth 3"],
            text=True, timeout=15,
        ).strip()
    except (_sp.CalledProcessError, _sp.TimeoutExpired, FileNotFoundError) as exc:
        print(f"  [evict] enumeration failed: {exc}", flush=True)
        return
    if not out:
        return
    parsed = json.loads(out)
    procs = parsed if isinstance(parsed, list) else [parsed]
    killed = []
    for p in procs:
        cmd = p.get("CommandLine") or ""
        m = _re.search(r"--port\s+(\d+)", cmd)
        port = m.group(1) if m else None
        if port in ("8090", "8091"):
            continue
        if "embedding.gguf" in cmd or "reranker.gguf" in cmd:
            continue
        pid = p.get("ProcessId")
        if not pid:
            continue
        try:
            _sp.run(["pwsh", "-NoProfile", "-Command",
                     f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"],
                    timeout=10, check=False)
            killed.append(f"pid={pid} port={port}")
        except (_sp.TimeoutExpired, OSError):
            pass
    if killed:
        print(f"  [evict] stopped {len(killed)} orphan llama-server(s): {killed}",
              flush=True)
        # Brief settle window so the residency manager observes the deaths
        # and the GPU driver releases the VRAM before the next spawn.
        time.sleep(5.0)


def _warm_tier_blocking(api_base: str, tier: str, *, max_wait_s: int = 300) -> bool:
    """Block until the tier's llama-server is loaded and producing real
    output. The /v1/chat/completions route currently returns 200 OK with
    empty content while the server is still spawning, so the bench cannot
    just rely on the first request's response — it has to retry until
    completion_tokens > 0. Returns True on success, False on timeout."""
    # Ensure prior tiers don't hog VRAM. Hard-kill orphan llama-servers
    # since the residency manager has been observed to wedge mid-eviction.
    _evict_other_llama_servers()
    import urllib.request as _ur, urllib.error as _ue
    body = json.dumps({
        "model": f"tier.{tier}",
        "messages": [{"role": "user", "content": "Say 'ready' and nothing else."}],
        "max_tokens": 8,
        "temperature": 0.0,
    }).encode("utf-8")
    deadline = time.time() + max_wait_s
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            req = _ur.Request(
                f"{api_base.rstrip('/')}/v1/chat/completions",
                data=body, headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _ur.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read())
            tokens = (payload.get("usage") or {}).get("completion_tokens", 0)
            content = ((payload.get("choices") or [{}])[0]
                       .get("message") or {}).get("content") or ""
            if tokens > 0 and content.strip():
                print(f"  [warm] {tier} ready after {attempt} attempt(s) "
                      f"({int(time.time() - (deadline - max_wait_s))}s, "
                      f"tokens={tokens})", flush=True)
                return True
        except (_ue.URLError, _ue.HTTPError, ValueError, OSError) as exc:
            print(f"  [warm] {tier} attempt {attempt}: {exc}", flush=True)
        time.sleep(2.0)
    print(f"  [warm] {tier} FAILED to warm in {max_wait_s}s", flush=True)
    return False


_CAT_TO_DATASET_KEY = {
    "knowledge": "mmlu",            "knowledge_specialized": "mmlu_pro",
    "math": "gsm8k",                "math_competition": "math",
    "reasoning": "aime2024",
    "coding": "humaneval",          "coding_basic": "mbpp",
    "intent": "ifeval",
    "clarity": "mtbench",
    "long_context": "needle",
}


def _evaluate_candidate(
    api_base: str,
    tier: str,
    overlay: dict,
    *,
    categories: list[str],
    n_per_category: int,
    think: bool,
    tools: str,
    max_tokens: int,
    timeout: int,
    depth: Depth,
) -> dict:
    """Run one candidate overlay across N problems per category. Returns
    {category: {n, passed, rate, wilson_lcb, mean_lat}, composite_lcb,
    composite_rate, total_wall_s}."""
    from backend.eval import datasets as _ds
    cat_results = {}
    total_wall = 0.0
    for cat in categories:
        rep_cap = _pt.CATEGORY_REPRESENTATIVES.get(cat, cat)
        ds_key = _CAT_TO_DATASET_KEY.get(rep_cap, rep_cap)
        # Skip categories whose dataset isn't vendored
        if ds_key not in _ds._DEPTHS:
            continue
        # Cap N at the available dataset size at this depth
        orig_n = _ds._DEPTHS[ds_key].get(depth, n_per_category)
        applied_n = min(n_per_category, orig_n)
        try:
            _ds._DEPTHS[ds_key][depth] = applied_n
            t0 = time.time()
            cell = run_cell(
                api_base, tier, rep_cap, depth,
                max_tokens=max_tokens,
                per_problem_timeout=timeout,
                think=think,
                tools=tools,
                sampling_overlay=overlay,
            )
            wall = time.time() - t0
        finally:
            _ds._DEPTHS[ds_key][depth] = orig_n
        n_real = max(1, cell.n_problems)
        passed = cell.n_passed
        cat_results[cat] = {
            "n": n_real,
            "passed": passed,
            "rate": round(passed / n_real, 4),
            "wilson_lcb": round(_pt.wilson_lcb(passed, n_real), 4),
            "mean_lat_s": round(cell.mean_latency_s, 2),
            "wall_s": round(wall, 1),
        }
        total_wall += wall
    if not cat_results:
        return {"composite_lcb": 0.0, "composite_rate": 0.0,
                "categories": {}, "total_wall_s": round(total_wall, 1)}
    avg_lcb = sum(v["wilson_lcb"] for v in cat_results.values()) / len(cat_results)
    avg_rate = sum(v["rate"] for v in cat_results.values()) / len(cat_results)
    return {
        "composite_lcb":  round(avg_lcb, 4),
        "composite_rate": round(avg_rate, 4),
        "categories":     cat_results,
        "total_wall_s":   round(total_wall, 1),
    }


def tune_one_tier_think(
    api_base: str,
    tier: str,
    *,
    think: bool,
    tools: str,
    categories: list[str],
    n_per_category: int,
    max_candidates: int,
    grid_param_names: list[str] | None,
    max_tokens: int,
    timeout: int,
    depth: Depth,
) -> dict:
    """Run the full grid for one (tier, think_mode), pick the best by
    composite Wilson-LCB, persist, return the audit dict."""
    grid = _pt.propose_grid(
        grid_param_names or list(_pt.TUNABLE_PARAMS.keys()),
        max_combos=max_candidates,
    )
    print(f"\n=== Tuning {tier}  think={think}  tools={tools} — "
          f"{len(grid)} candidates × {len(categories)} categories × n={n_per_category}",
          flush=True)
    explored = []
    best = None
    for i, overlay in enumerate(grid, 1):
        cap_label = ", ".join(f"{k}={v}" for k, v in overlay.items())
        print(f"  [{i}/{len(grid)}] {cap_label}", flush=True)
        result = _evaluate_candidate(
            api_base, tier, overlay,
            categories=categories,
            n_per_category=n_per_category,
            think=think,
            tools=tools,
            max_tokens=max_tokens,
            timeout=timeout,
            depth=depth,
        )
        per_cat = "  ".join(
            f"{c}={int(v['rate']*100)}%(LCB{v['wilson_lcb']:.2f})"
            for c, v in result["categories"].items()
        )
        print(f"    -> composite LCB {result['composite_lcb']:.3f}  "
              f"rate {int(result['composite_rate']*100)}%  "
              f"[{per_cat}]  wall {int(result['total_wall_s'])}s",
              flush=True)
        explored.append({"overlay": overlay, **result})
        if best is None or result["composite_lcb"] > best["composite_lcb"]:
            best = {"overlay": overlay, **result}

    if best is not None:
        # Determine total samples for the persisted overlay
        n_total = sum(v["n"] for v in best["categories"].values())
        _pt.update_overlay(
            tier=tier, capability="composite",
            overlay=best["overlay"],
            score=best["composite_lcb"],
            n_samples=n_total,
            think=think,
            composite_breakdown=best["categories"],
            explored=explored,
        )
        # Compare against the default-overlay candidate if explored
        default_overlay = {n: _pt.TUNABLE_PARAMS[n].default for n in best["overlay"]}
        default_in_grid = next(
            (e for e in explored
             if all(e["overlay"][k] == default_overlay[k] for k in default_overlay)),
            None,
        )
        delta = ""
        if default_in_grid is not None:
            delta = (f"  delta_LCB="
                     f"{(best['composite_lcb'] - default_in_grid['composite_lcb']):+.3f}  "
                     f"delta_rate={(best['composite_rate'] - default_in_grid['composite_rate'])*100:+.1f}pp")
        print(f"  [BEST] composite LCB={best['composite_lcb']:.3f}  "
              f"rate={int(best['composite_rate']*100)}%{delta}", flush=True)
    return {"best": best, "explored": explored}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--api", default="http://127.0.0.1:18000")
    p.add_argument("--tiers", required=True)
    p.add_argument("--think", default="off",
                   help="Comma-separated 'off' / 'on' / 'off,on'.")
    p.add_argument("--tools", default="off")
    p.add_argument("--categories", default="knowledge,math,coding,tool_use",
                   help="Categories to score against (composite mean of these). "
                        "Available: knowledge, math, reasoning, coding, tool_use, clarity.")
    p.add_argument("--depth", default="fast")
    p.add_argument("--n-per-category", type=int, default=30)
    p.add_argument("--max-candidates", type=int, default=6)
    p.add_argument("--max-tokens", type=int, default=4096)
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--params", default=None)
    p.add_argument("--out-audit", default=None)
    args = p.parse_args()

    # Wire into the unified eval log so bench_progress.py can render a
    # live dashboard for the tuner the same way it does for full benches.
    from backend import observability as _obs
    _obs.install("eval", suffix="tune", extra_state={
        "kind": "param-tuner",
        "tiers": args.tiers,
        "think": args.think,
        "categories": args.categories,
        "n_per_category": args.n_per_category,
        "max_candidates": args.max_candidates,
    })

    tiers = [t.strip() for t in args.tiers.split(",") if t.strip()]
    think_modes = [t.strip() for t in args.think.split(",") if t.strip()]
    categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    grid_param_names = [s.strip() for s in args.params.split(",")] if args.params else None

    audit: dict = {
        "started": datetime.now().isoformat(timespec="seconds"),
        "args": vars(args),
        "results": {},
    }
    for tier in tiers:
        # Synchronously warm the tier before sending bench requests.
        # The /v1/chat/completions route returns 200/empty while the
        # llama-server is still spawning, which silently zeroes out
        # all candidate scores until the model finishes loading.
        if not _warm_tier_blocking(args.api, tier, max_wait_s=600):
            print(f"  FAIL {tier}: tier never warmed up", file=sys.stderr)
            for think_label in think_modes:
                key = f"{tier}/think_{think_label}"
                audit["results"][key] = {"error": "tier-failed-to-warm"}
            continue
        for think_label in think_modes:
            think = {"off": False, "on": True}[think_label]
            key = f"{tier}/think_{think_label}"
            try:
                result = tune_one_tier_think(
                    args.api, tier,
                    think=think, tools=args.tools,
                    categories=categories,
                    n_per_category=args.n_per_category,
                    max_candidates=args.max_candidates,
                    grid_param_names=grid_param_names,
                    max_tokens=args.max_tokens,
                    timeout=args.timeout,
                    depth=args.depth,
                )
                audit["results"][key] = result
            except Exception as exc:
                print(f"  FAIL {key}: {exc}", file=sys.stderr)
                import traceback; traceback.print_exc()
                audit["results"][key] = {"error": str(exc)}

    audit["finished"] = datetime.now().isoformat(timespec="seconds")
    out_path = Path(args.out_audit) if args.out_audit else (
        Path(__file__).resolve().parent.parent / "data" / "eval"
        / f"tuning-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    print(f"\nAudit log: {out_path}")
    print(f"Persisted overlays: data/eval/tuned_params.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
