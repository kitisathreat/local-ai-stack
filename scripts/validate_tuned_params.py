"""Stage-2 validation: re-bench (best overlay) vs (defaults overlay) at
high n, then run a two-proportion z-test per (tier, think_mode).

The stage-1 tuner (``scripts/tune_bench_params.py``) screens a small
candidate grid at n=30/category to find the winner cheaply. Its picks
are directionally informative but rarely statistically significant —
at n=120/arm a 3-5pp gap typically lands at z<1, well below the 1.96
two-sided threshold.

This script takes the persisted winner per (tier, think) and re-benches
just two arms (best vs defaults) at much higher n, so the z-test has
enough power to either confirm dynamic tuning beats the static baseline
or flag the gap as noise.

Pipeline
--------
For each (tier, think_mode) entry in tuned_params.json:
  1. Read the best overlay from ``by_tier_think[<tier>/think_<mode>]``.
  2. Construct the defaults overlay from ``TUNABLE_PARAMS[*].default``.
  3. Run ``--n-per-category`` problems for both arms across the same
     4 categories used in stage 1.
  4. Pool counts across all categories (matching the composite metric).
  5. Two-proportion z-test with pooled variance, α=0.05 two-sided.
  6. Persist the result to ``data/eval/validation-<ts>.json`` and print
     a summary table.

Usage
-----
    python scripts/validate_tuned_params.py \\
        --tiers swarm,fast,versatile,coding,highest_quality \\
        --think off \\
        --n-per-category 300
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.eval import param_tuning as _pt
from backend.eval import datasets as _ds
from backend.eval.runner import run_cell


# Reuse the screen-time mapping so categories evaluate the same dataset
# in stage 2 as in stage 1.
_CAT_TO_DATASET_KEY = {
    "knowledge": "mmlu", "math": "gsm8k", "coding": "humaneval",
    "tool_use": "ifeval",
}


def _two_prop_z(a_pass: int, a_n: int, b_pass: int, b_n: int) -> dict:
    """Pooled two-proportion z-test. Returns {z, p_two_sided, sig_05}."""
    if a_n <= 0 or b_n <= 0:
        return {"z": 0.0, "p_two_sided": 1.0, "sig_05": False}
    p_a = a_pass / a_n
    p_b = b_pass / b_n
    p_pool = (a_pass + b_pass) / (a_n + b_n)
    var = p_pool * (1 - p_pool) * (1 / a_n + 1 / b_n)
    se = math.sqrt(var) if var > 0 else 0.0
    if se == 0:
        return {"z": 0.0, "p_two_sided": 1.0, "sig_05": False, "diff_pp": 0.0}
    z = (p_a - p_b) / se
    # Two-sided p-value via standard normal CDF (no scipy dep)
    p_two_sided = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(z) / math.sqrt(2))))
    return {
        "z": round(z, 3),
        "p_two_sided": round(p_two_sided, 4),
        "sig_05": bool(abs(z) >= 1.96),
        "diff_pp": round((p_a - p_b) * 100, 2),
        "p_a": round(p_a, 4),
        "p_b": round(p_b, 4),
    }


def _bench_overlay(api: str, tier: str, overlay: dict, *,
                   categories: list[str], n_per_cat: int, think: bool,
                   max_tokens: int, timeout: int, depth: str) -> dict:
    """Run all categories for one overlay; return pooled (passed, n) plus
    per-category breakdown."""
    per_cat = {}
    pooled_pass = 0
    pooled_n = 0
    total_wall = 0.0
    for cat in categories:
        rep_cap = _pt.CATEGORY_REPRESENTATIVES.get(cat, cat)
        ds_key = _CAT_TO_DATASET_KEY.get(cat, rep_cap)
        if ds_key not in _ds._DEPTHS:
            continue
        orig_n = _ds._DEPTHS[ds_key].get(depth, n_per_cat)
        applied_n = min(n_per_cat, orig_n)
        try:
            _ds._DEPTHS[ds_key][depth] = applied_n
            t0 = time.time()
            cell = run_cell(
                api, tier, rep_cap, depth,
                max_tokens=max_tokens,
                per_problem_timeout=timeout,
                think=think,
                tools="off",
                sampling_overlay=overlay,
            )
            wall = time.time() - t0
        finally:
            _ds._DEPTHS[ds_key][depth] = orig_n
        per_cat[cat] = {
            "n": cell.n_problems, "passed": cell.n_passed,
            "rate": round(cell.n_passed / max(1, cell.n_problems), 4),
            "wall_s": round(wall, 1),
        }
        pooled_pass += cell.n_passed
        pooled_n += cell.n_problems
        total_wall += wall
    return {
        "pooled_passed": pooled_pass,
        "pooled_n": pooled_n,
        "categories": per_cat,
        "wall_s": round(total_wall, 1),
    }


def _evict_other_llama_servers() -> None:
    """Hard-kill llama-server.exe processes other than embedding/reranker.
    Residency manager wedges between tier swaps; explicit kill is safer."""
    import subprocess as _sp, re as _re
    try:
        out = _sp.check_output(
            ["pwsh", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"name='llama-server.exe'\" | "
             "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress -Depth 3"],
            text=True, timeout=15,
        ).strip()
    except (_sp.CalledProcessError, _sp.TimeoutExpired, FileNotFoundError):
        return
    if not out:
        return
    parsed = json.loads(out)
    procs = parsed if isinstance(parsed, list) else [parsed]
    killed = 0
    for p in procs:
        cmd = p.get("CommandLine") or ""
        m = _re.search(r"--port\s+(\d+)", cmd)
        port = m.group(1) if m else None
        if port in ("8090", "8091") or "embedding.gguf" in cmd or "reranker.gguf" in cmd:
            continue
        pid = p.get("ProcessId")
        if not pid:
            continue
        try:
            _sp.run(["pwsh", "-NoProfile", "-Command",
                     f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"],
                    timeout=10, check=False)
            killed += 1
        except (_sp.TimeoutExpired, OSError):
            pass
    if killed:
        print(f"  [evict] stopped {killed} orphan llama-server(s)", flush=True)
        time.sleep(5.0)


def _warm(api: str, tier: str, max_wait_s: int = 600) -> bool:
    """Same blocking warm-up as the stage-1 tuner: probe until non-empty
    response so the empty-content-during-spawn race doesn't zero a run."""
    _evict_other_llama_servers()
    import urllib.request as _ur, urllib.error as _ue
    body = json.dumps({
        "model": f"tier.{tier}",
        "messages": [{"role": "user", "content": "Say 'ready'."}],
        "max_tokens": 8, "temperature": 0.0,
    }).encode()
    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        try:
            with _ur.urlopen(_ur.Request(
                f"{api.rstrip('/')}/v1/chat/completions", data=body,
                headers={"Content-Type": "application/json"}, method="POST"
            ), timeout=60) as resp:
                payload = json.loads(resp.read())
            if (payload.get("usage") or {}).get("completion_tokens", 0) > 0:
                return True
        except (_ue.URLError, _ue.HTTPError, ValueError, OSError):
            pass
        time.sleep(2.0)
    return False


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--api", default="http://127.0.0.1:18000")
    p.add_argument("--tiers", required=True)
    p.add_argument("--think", default="off")
    p.add_argument("--categories", default="knowledge,math,coding,tool_use")
    p.add_argument("--depth", default="fast")
    p.add_argument("--n-per-category", type=int, default=300,
                   help="Higher than stage 1 — needed for stat-sig power.")
    p.add_argument("--max-tokens", type=int, default=4096)
    p.add_argument("--timeout", type=int, default=300)
    args = p.parse_args()

    # Wire into the unified eval log so bench_progress.py renders this.
    from backend import observability as _obs
    _obs.install("eval", suffix="validate", extra_state={
        "kind": "param-validator", "tiers": args.tiers,
        "n_per_category": args.n_per_category,
    })

    tiers = [t.strip() for t in args.tiers.split(",") if t.strip()]
    think_modes = [t.strip() for t in args.think.split(",") if t.strip()]
    categories = [c.strip() for c in args.categories.split(",") if c.strip()]

    tuned = _pt.load_all().get("by_tier_think", {})
    defaults = {n: _pt.TUNABLE_PARAMS[n].default for n in _pt.TUNABLE_PARAMS}

    audit = {"started": datetime.now().isoformat(timespec="seconds"),
             "args": vars(args), "results": {}}

    for tier in tiers:
        if not _warm(args.api, tier):
            print(f"FAIL warm {tier}", file=sys.stderr)
            for tm in think_modes:
                audit["results"][f"{tier}/think_{tm}"] = {"error": "warm-failed"}
            continue
        for tm in think_modes:
            think = {"off": False, "on": True}[tm]
            key = f"{tier}/think_{tm}"
            entry = tuned.get(key)
            if not entry:
                print(f"SKIP {key}: no tuned overlay", file=sys.stderr)
                audit["results"][key] = {"error": "no-tuned-overlay"}
                continue
            best = {k: v for k, v in entry.items() if not k.startswith("_")}
            print(f"\n=== Validating {key} ===", flush=True)
            print(f"  best:    {best}", flush=True)
            print(f"  default: {defaults}", flush=True)

            print(f"  -> running BEST overlay (n={args.n_per_category}/cat)…", flush=True)
            best_res = _bench_overlay(
                args.api, tier, best,
                categories=categories,
                n_per_cat=args.n_per_category,
                think=think,
                max_tokens=args.max_tokens,
                timeout=args.timeout,
                depth=args.depth,
            )
            print(f"     best:    {best_res['pooled_passed']}/{best_res['pooled_n']} "
                  f"({100*best_res['pooled_passed']/max(1,best_res['pooled_n']):.1f}%)  "
                  f"wall {best_res['wall_s']:.0f}s", flush=True)

            print(f"  -> running DEFAULT overlay (n={args.n_per_category}/cat)…", flush=True)
            def_res = _bench_overlay(
                args.api, tier, defaults,
                categories=categories,
                n_per_cat=args.n_per_category,
                think=think,
                max_tokens=args.max_tokens,
                timeout=args.timeout,
                depth=args.depth,
            )
            print(f"     default: {def_res['pooled_passed']}/{def_res['pooled_n']} "
                  f"({100*def_res['pooled_passed']/max(1,def_res['pooled_n']):.1f}%)  "
                  f"wall {def_res['wall_s']:.0f}s", flush=True)

            ztest = _two_prop_z(
                best_res["pooled_passed"], best_res["pooled_n"],
                def_res["pooled_passed"], def_res["pooled_n"],
            )
            verdict = "STAT-SIG" if ztest["sig_05"] else "not stat-sig"
            print(f"  -> z={ztest['z']}  p={ztest['p_two_sided']}  "
                  f"diff={ztest['diff_pp']:+.2f}pp  [{verdict}]", flush=True)

            audit["results"][key] = {
                "best_overlay": best, "default_overlay": defaults,
                "best": best_res, "default": def_res, "ztest": ztest,
            }

    audit["finished"] = datetime.now().isoformat(timespec="seconds")
    out = (Path(__file__).resolve().parent.parent / "data" / "eval"
           / f"validation-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    print(f"\nValidation audit: {out}", flush=True)

    print("\n=== Summary ===")
    print(f"{'tier/think':<32}  {'best%':>6}  {'def%':>6}  {'Δpp':>6}  {'z':>5}  {'p':>6}  {'verdict':<12}")
    for k, v in audit["results"].items():
        if "ztest" not in v:
            print(f"{k:<32}  {'—':>6}  {'—':>6}  {'—':>6}  {'—':>5}  {'—':>6}  {v.get('error','?'):<12}")
            continue
        zt = v["ztest"]
        print(f"{k:<32}  {zt['p_a']*100:6.1f}  {zt['p_b']*100:6.1f}  "
              f"{zt['diff_pp']:+6.2f}  {zt['z']:5.2f}  {zt['p_two_sided']:6.4f}  "
              f"{'STAT-SIG' if zt['sig_05'] else 'not sig':<12}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
