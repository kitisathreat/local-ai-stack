"""Run the full extended capability bench: 9 capabilities × variable depth
per tier × both think modes.

Depth scales with tier weight class:
  - Small (swarm, fast, fast_r1_distill, fast_phi4) → fast
  - Mid MoE (versatile, coding) → medium
  - Big MoE (highest_quality, reasoning_max, reasoning_xl, frontier) → full

Capabilities: reasoning, math, math_competition, coding, coding_basic,
knowledge, knowledge_specialized, intent, clarity, long_context.

The clarity grader uses `highest_quality` as the LLM judge (set via
`backend.eval.graders.MTBENCH_JUDGE_TIER`).

Output: one JSON per (think_mode), aggregated across all tiers.

Usage:
    python scripts/run_full_bench.py
    python scripts/run_full_bench.py --think off,on
    python scripts/run_full_bench.py --judge-tier reasoning_max
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _evict_other_llama_servers() -> None:
    """Hard-kill llama-server.exe processes other than embedding/reranker.
    Residency manager wedges between tier swaps; explicit kill is safer."""
    import subprocess as _sp, re as _re, json as _json
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
    parsed = _json.loads(out)
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
        print(f"  [evict {datetime.now().strftime('%H:%M:%S')}] "
              f"stopped {killed} orphan llama-server(s)", flush=True)
        time.sleep(5.0)


def _warm_tier_blocking(api_base: str, tier: str, *, max_wait_s: int = 600) -> bool:
    """Probe the tier's chat endpoint until completion_tokens > 0. The
    /v1/chat/completions route returns 200 OK with empty content while
    the llama-server is still spawning, so the bench cannot just rely on
    the first request's response — has to retry until non-empty."""
    import urllib.request as _ur, urllib.error as _ue, json as _json
    body = _json.dumps({
        "model": f"tier.{tier}",
        "messages": [{"role": "user", "content": "Say 'ready' and nothing else."}],
        "max_tokens": 8, "temperature": 0.0,
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
            with _ur.urlopen(req, timeout=120) as resp:
                payload = _json.loads(resp.read())
            tokens = (payload.get("usage") or {}).get("completion_tokens", 0)
            content = ((payload.get("choices") or [{}])[0]
                       .get("message") or {}).get("content") or ""
            if tokens > 0 and content.strip():
                print(f"  [warm {datetime.now().strftime('%H:%M:%S')}] "
                      f"{tier} ready after {attempt} attempt(s) tokens={tokens}",
                      flush=True)
                return True
        except (_ue.URLError, _ue.HTTPError, ValueError, OSError) as exc:
            if attempt % 5 == 1:
                print(f"  [warm {datetime.now().strftime('%H:%M:%S')}] "
                      f"{tier} attempt {attempt}: {exc}", flush=True)
        time.sleep(3.0)
    print(f"  [warm {datetime.now().strftime('%H:%M:%S')}] "
          f"{tier} FAILED to warm in {max_wait_s}s", flush=True)
    return False


TIER_DEPTH = {
    "swarm":              "fast",
    "fast":               "fast",
    "fast_r1_distill":    "fast",
    "fast_phi4":          "fast",
    "versatile":          "medium",
    "coding":             "medium",
    "highest_quality":    "full",
    "reasoning_max":      "full",
    "reasoning_xl":       "full",
    "frontier":           "full",
}

# Mirror of `context_window` per tier in config/models.yaml. Used by
# run_cell to skip needle problems whose ctx_target exceeds the tier's
# capacity (otherwise llama-server returns empty content for over-budget
# prompts and the cell's pass-rate becomes a methodology artifact).
TIER_CONTEXT_WINDOW = {
    "swarm":              16384,
    "fast":               65536,
    "fast_r1_distill":    65536,
    "fast_phi4":          32768,
    "versatile":          65536,
    "coding":             131072,
    "highest_quality":    32768,
    "reasoning_max":      32768,
    "reasoning_xl":       65536,
    "frontier":           32768,
    "vision":             65536,
}

# When --target=significance the depth picker forces stat_sig (or
# stat_sig_strict) for every tier so each cell hits the N needed for a
# 95% CI ±7pp (or ±5pp) point estimate. Per-tier depth overrides are
# ignored in that mode — significance trumps wall-clock.

DEFAULT_CAPABILITIES = [
    "knowledge",
    "knowledge_specialized",
    "math",
    "math_competition",
    "reasoning",
    "coding",
    "coding_basic",
    "intent",
    "clarity",
    "long_context",
]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--api", default="http://127.0.0.1:18000")
    p.add_argument("--tiers", default="swarm,fast,versatile,coding,highest_quality,reasoning_max",
                   help="Comma-separated tier list. Excludes thinking-default-empty tiers by default.")
    p.add_argument("--capabilities", default=",".join(DEFAULT_CAPABILITIES))
    p.add_argument("--think", default="off,on",
                   help="Comma-separated: 'off' / 'on' / 'off,on' for both.")
    p.add_argument("--tools", default="off,auto",
                   help="Comma-separated: 'off' / 'auto' / 'force'. 'auto' = "
                        "two-pass: try without tools, retry with tools when "
                        "the model's first response looks inadequate.")
    p.add_argument("--judge-tier", default="highest_quality",
                   help="Tier used as LLM judge for clarity scoring.")
    p.add_argument("--max-tokens", type=int, default=16384)
    p.add_argument("--per-problem-timeout", type=int, default=900)
    p.add_argument("--out-dir", default="data/eval/results")
    p.add_argument("--resume-from", default=None,
                   help="Path to a previous cumulative JSON. Cells already "
                        "complete (full N, no abort_reason) are skipped. "
                        "Aborted or missing cells are re-run. Useful when a "
                        "tier crashes mid-suite.")
    p.add_argument("--multi-agent-tiers", default="swarm,fast",
                   help="Comma-separated worker tiers to bench in multi-agent "
                        "mode after the single-agent suite finishes. Each tier "
                        "becomes a virtual cell with multi_agent=true and the "
                        "default orchestrator (versatile). Empty string disables.")
    p.add_argument("--multi-agent-orchestrator", default="versatile",
                   help="Orchestrator tier for the multi-agent post-suite.")
    p.add_argument("--target", default="count",
                   choices=("count", "time", "significance", "significance_strict"),
                   help="Bench length target. 'count' uses TIER_DEPTH (small=fast, "
                        "mid=medium, big=full). 'time' (with --target-minutes) caps "
                        "wall time. 'significance' uses depth=stat_sig + early-stop "
                        "when Wilson 95%-CI half-width ≤ 5pp. 'significance_strict' "
                        "raises N cap to 385 and tightens early-stop to ≤3pp.")
    p.add_argument("--target-minutes", type=int, default=0,
                   help="With --target=time: max wall-clock minutes. 0 = no cap.")
    p.add_argument("--margin", type=float, default=None,
                   help="Override Wilson-CI half-width target (default: 0.05 for "
                        "significance, 0.03 for significance_strict).")
    p.add_argument("--confidence", type=float, default=0.95,
                   help="CI confidence level (0.90/0.95/0.99 supported, default 0.95).")
    args = p.parse_args()

    # Wire the LLM-judge config before anything calls into graders.
    import backend.eval.graders as _g
    _g.MTBENCH_JUDGE_API = args.api
    _g.MTBENCH_JUDGE_TIER = args.judge_tier

    from backend.eval.runner import CAPABILITIES, run_cell, write_json, write_markdown
    from backend import observability as obs
    obs.install("eval")

    tiers = [t.strip() for t in args.tiers.split(",") if t.strip()]
    capabilities = [c.strip() for c in args.capabilities.split(",") if c.strip()]
    think_modes = [t.strip() for t in args.think.split(",") if t.strip()]
    tools_modes = [t.strip() for t in args.tools.split(",") if t.strip()]

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    n_conditions = len(think_modes) * len(tools_modes)
    print(f"Full bench: {len(tiers)} tier × {len(capabilities)} cap × {n_conditions} condition(s) "
          f"(think={think_modes}, tools={tools_modes})")
    print(f"  judge tier: {args.judge_tier}")
    print(f"  per-tier depth: {[(t, TIER_DEPTH.get(t, 'fast')) for t in tiers]}")
    print()

    # Resolve depth per-tier based on --target.
    def _depth_for(tier: str) -> str:
        if args.target == "significance":
            return "stat_sig"
        if args.target == "significance_strict":
            return "stat_sig_strict"
        if args.target == "time":
            # Use per-tier-class depth but the loop will short-circuit
            # when wall time runs out.
            return TIER_DEPTH.get(tier, "fast")
        return TIER_DEPTH.get(tier, "fast")

    # Wilson-CI early-stop margin: with significance modes, run_cell
    # terminates each cell as soon as the running pass-rate's CI is
    # tight enough — much faster than running the full N when results
    # are decisive (p ~ 0 or 1) while still hitting the requested N
    # when results are noisy near 50/50.
    early_stop_margin = args.margin
    if early_stop_margin is None:
        if args.target == "significance":
            early_stop_margin = 0.05
        elif args.target == "significance_strict":
            early_stop_margin = 0.03

    deadline = None
    if args.target == "time" and args.target_minutes > 0:
        deadline = time.time() + args.target_minutes * 60

    print(f"Target: {args.target}"
          + (f" ({args.target_minutes} min)" if args.target == "time" else "")
          + (f"  early-stop margin={early_stop_margin:.2f}@{args.confidence:.2f}"
             if early_stop_margin else ""))
    print()

    # Tier-grouped loop: complete every (think, tools, capability) for tier T
    # before evicting and moving to tier T+1. Reduces tier-swap overhead
    # (each swap costs 5–60s) and means the dashboard can show "all swarm
    # done" before any fast cell starts. Per-cell results stream to one
    # cumulative JSON so the dashboard updates after every cell.
    cumulative_path = out_dir / f"full-bench-{ts}-cumulative.json"
    all_results: list = []
    run_started = time.time()
    total_cells = len(tiers) * len(capabilities) * len(think_modes) * len(tools_modes)
    cells_done = 0

    # Resume support: load existing cumulative JSON, mark "good" cells
    # (full N + no abort_reason) so the main loop skips them. Re-runs
    # aborted cells and any cell not present.
    skip_set: set = set()
    if args.resume_from:
        import json as _json
        try:
            prev_doc = _json.loads(Path(args.resume_from).read_text(encoding="utf-8"))
            # write_json wraps results: {"schema_version", "written_at", "results": [...]}
            prev = prev_doc.get("results", prev_doc) if isinstance(prev_doc, dict) else prev_doc
            for entry in prev:
                # entry is a serialized TierResult dict
                tier_e = entry.get("tier")
                cap_e = entry.get("capability")
                think_e = entry.get("think")
                tools_e = entry.get("tools")
                # Older runs (before think/tools were persisted in the
                # dataclass) wrote None — fall back to off/off, which
                # matches the bench loop's first iteration.
                if isinstance(think_e, bool):
                    think_label_e = "on" if think_e else "off"
                elif think_e is None:
                    think_label_e = "off"
                else:
                    think_label_e = think_e
                if tools_e is None:
                    tools_e = "off"
                aborted = entry.get("abort_reason")
                n_problems_e = entry.get("n_problems", 0)
                # Keep only cells that completed without abort AND have a
                # substantive sample size. Older runs (before abort_reason
                # was persisted) had None for that field even when aborted,
                # so fall back to a wall-time + problem-count heuristic:
                # cells under 30 problems with <30s wall are almost
                # certainly mid-cell aborts (5 consecutive tok=0 detector).
                wall_sec = entry.get("finished_at", 0) - entry.get("started_at", 0)
                looks_aborted = (
                    n_problems_e < 30 and wall_sec < 30
                )
                if not aborted and not looks_aborted and n_problems_e >= 30:
                    key = (tier_e, cap_e, think_label_e, tools_e)
                    skip_set.add(key)
                    # Carry forward into the new cumulative JSON so the
                    # dashboard sees a continuous picture.
                    from backend.eval.runner import TierResult, ProblemResult
                    tr = TierResult(
                        tier=tier_e,
                        capability=cap_e,
                        depth=entry.get("depth", "stat_sig"),
                        started_at=entry.get("started_at", run_started),
                        finished_at=entry.get("finished_at", run_started),
                        n_problems=n_problems_e,
                        n_passed=entry.get("n_passed", 0),
                        pass_rate=entry.get("pass_rate", 0.0),
                        mean_latency_s=entry.get("mean_latency_s", 0.0),
                        p95_latency_s=entry.get("p95_latency_s", 0.0),
                        problems=[],
                        n_skipped_ctx=entry.get("n_skipped_ctx", 0),
                        skipped_ids=entry.get("skipped_ids", []),
                        abort_reason=None,
                        # Preserve think/tools markers so the dashboard
                        # heatmap can distinguish (think=on, tools=off)
                        # from (think=off, tools=off). Without this the
                        # carried-forward cells show up as think=None
                        # and collide with new cells in the same row.
                        think=think_label_e,
                        tools=tools_e,
                    )
                    all_results.append(tr)
                    cells_done += 1
            print(f"  [resume] loaded {len(skip_set)} completed cells from "
                  f"{args.resume_from}", flush=True)
            # Persist the carry-forward immediately so dashboard sees them
            write_json(all_results, cumulative_path)
            write_markdown(all_results, cumulative_path.with_suffix(".md"))
        except Exception as exc:
            print(f"  [resume] failed to load {args.resume_from}: {exc}",
                  flush=True)

    for tier in tiers:
        # Per-tier warm-up: block synchronously until the new tier responds
        # with non-empty content. The scheduler handles tier eviction via
        # its own acquire() path; killing llama-server processes externally
        # leaves the scheduler's loaded dict stale (phantom-tracked VRAM)
        # which then rejects subsequent loads with VRAMExhausted.
        if not _warm_tier_blocking(args.api, tier):
            print(f"  FAIL warm {tier} — skipping all cells for this tier",
                  flush=True)
            continue
        depth = _depth_for(tier)
        tier_started = time.time()
        for tools_label in tools_modes:
            for think_label in think_modes:
                think = {"off": False, "on": True}[think_label]
                for cap in capabilities:
                    if deadline and time.time() > deadline:
                        print(f"  --target=time deadline hit, stopping after "
                              f"{(time.time()-run_started)/60:.1f} min", flush=True)
                        break
                    if (tier, cap, think_label, tools_label) in skip_set:
                        print(f"  [{datetime.now().strftime('%H:%M:%S')}] "
                              f"[resume-skip] {tier} × {cap} × think={think_label} × "
                              f"tools={tools_label} already complete", flush=True)
                        continue
                    print(f"  [{datetime.now().strftime('%H:%M:%S')}] "
                          f"Running {tier} × {cap} × think={think_label} × tools={tools_label} "
                          f"(depth={depth})…", flush=True)
                    t0 = time.time()
                    cell = run_cell(
                        args.api, tier, cap, depth,
                        max_tokens=args.max_tokens,
                        per_problem_timeout=args.per_problem_timeout,
                        think=think,
                        tools=tools_label,
                        early_stop_margin=early_stop_margin,
                        early_stop_confidence=args.confidence,
                        tier_context_window=TIER_CONTEXT_WINDOW.get(tier),
                    )
                    wall = time.time() - t0
                    cells_done += 1
                    pct = 100 * cells_done / max(1, total_cells)
                    # Type I (alpha) + Type II (power/MDE) annotations
                    import math as _math
                    n = max(1, cell.n_problems)
                    p = cell.pass_rate
                    # Wilson 95% CI half-width (single-cell precision)
                    z = 1.96
                    z2 = z * z
                    denom = 1.0 + z2 / n
                    half = (z * _math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))) / denom
                    # Two-proportion z-test: at alpha=0.05 two-sided, beta=0.20
                    # (80% power), equal n per arm, baseline = observed p, the
                    # minimum detectable effect (MDE) is roughly:
                    #     MDE = (z_{1-a/2} + z_{1-b}) * sqrt(2*p*(1-p)/n)
                    z_a = 1.96   # alpha=0.05 two-sided
                    z_b = 0.84   # 80% power
                    var = max(0.0, p * (1 - p) * 2 / n)
                    mde = (z_a + z_b) * _math.sqrt(var)
                    skipped_note = ""
                    if getattr(cell, "n_skipped_ctx", 0):
                        skipped_note = f" [skipped {cell.n_skipped_ctx} ctx-overflow]"
                    print(f"    [{datetime.now().strftime('%H:%M:%S')}] "
                          f"-> pass {cell.n_passed}/{n} = {p*100:.1f}% "
                          f"(95% CI ±{half*100:.1f}pp, MDE@80% pwr ±{mde*100:.1f}pp) "
                          f"in {wall:.0f}s (mean lat {cell.mean_latency_s:.1f}s) "
                          f"[{cells_done}/{total_cells} = {pct:.1f}%]{skipped_note}",
                          flush=True)
                    all_results.append(cell)
                    # Incremental persistence — dashboard reads this file
                    # after every cell so progress is live.
                    write_json(all_results, cumulative_path)
                    write_markdown(all_results, cumulative_path.with_suffix(".md"))
                    # Liveness recovery: if the runner aborted this cell
                    # (consecutive tok=0 cascade) OR mean latency is
                    # implausibly low, the tier's llama-server has almost
                    # certainly crashed. Per methodology, we PAUSE — re-warm
                    # blocks here until the tier responds with non-empty
                    # content, then we re-run the just-aborted cell BEFORE
                    # advancing. The skip_set marker for resume mode is
                    # cleared so we re-evaluate this (tier,cap) cleanly.
                    aborted = (
                        getattr(cell, "abort_reason", None) is not None
                        or (cell.mean_latency_s < 0.2 and cell.n_problems > 0)
                    )
                    if aborted:
                        reason = getattr(cell, "abort_reason", None) or "low_latency"
                        print(f"    [{datetime.now().strftime('%H:%M:%S')}] "
                              f"!! tier {tier} cell aborted ({reason}); "
                              f"re-warming and retrying this cell", flush=True)
                        # Drop the bad cell record so it's not persisted
                        # alongside the eventual valid one.
                        try:
                            all_results.pop()
                            cells_done = max(0, cells_done - 1)
                        except (IndexError, NameError):
                            pass
                        if not _warm_tier_blocking(args.api, tier, max_wait_s=900):
                            print(f"    !! re-warm failed; skipping rest of "
                                  f"tier {tier}", flush=True)
                            break
                        # Retry this cell once more with the freshly-warmed
                        # tier. If it aborts again, we move on (rather than
                        # loop forever) — the operator can resume later.
                        print(f"    [{datetime.now().strftime('%H:%M:%S')}] "
                              f"retrying {tier} × {cap} × think={think_label} × "
                              f"tools={tools_label}", flush=True)
                        t0 = time.time()
                        cell = run_cell(
                            args.api, tier, cap, depth,
                            max_tokens=args.max_tokens,
                            per_problem_timeout=args.per_problem_timeout,
                            think=think,
                            tools=tools_label,
                            early_stop_margin=early_stop_margin,
                            early_stop_confidence=args.confidence,
                            tier_context_window=TIER_CONTEXT_WINDOW.get(tier),
                        )
                        wall = time.time() - t0
                        cells_done += 1
                        n = max(1, cell.n_problems)
                        p = cell.pass_rate
                        denom = 1.0 + z2 / n
                        half = (z * _math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))) / denom
                        var = max(0.0, p * (1 - p) * 2 / n)
                        mde = (z_a + z_b) * _math.sqrt(var)
                        skipped_note = ""
                        if getattr(cell, "n_skipped_ctx", 0):
                            skipped_note = f" [skipped {cell.n_skipped_ctx} ctx-overflow]"
                        retry_note = f" {{retry}}"
                        if getattr(cell, "abort_reason", None):
                            retry_note = f" {{retry-aborted-again:{cell.abort_reason}}}"
                        print(f"    [{datetime.now().strftime('%H:%M:%S')}] "
                              f"-> pass {cell.n_passed}/{n} = {p*100:.1f}% "
                              f"(95% CI ±{half*100:.1f}pp, MDE@80% pwr ±{mde*100:.1f}pp) "
                              f"in {wall:.0f}s (mean lat {cell.mean_latency_s:.1f}s) "
                              f"[{cells_done}/{total_cells} = {100*cells_done/max(1,total_cells):.1f}%]"
                              f"{skipped_note}{retry_note}", flush=True)
                        all_results.append(cell)
                        write_json(all_results, cumulative_path)
                        write_markdown(all_results, cumulative_path.with_suffix(".md"))

        tier_wall = time.time() - tier_started
        print(f"\n=== tier {tier} done in {tier_wall/60:.1f} min ===\n", flush=True)

    total_wall = time.time() - run_started
    print(f"\n=== ALL TIERS done in {total_wall/60:.1f} min "
          f"({cells_done}/{total_cells} cells) ===")
    print(f"  JSON: {cumulative_path}")
    print(f"  MD:   {cumulative_path.with_suffix('.md')}")

    # ── Multi-agent post-suite ─────────────────────────────────────────
    # Run multi-agent benches with the requested worker tiers (default:
    # swarm, fast — the "light" tiers most useful as parallel workers).
    # Each problem fans the request out via the multi-agent dispatcher
    # (orchestrator + N workers + synthesis), so per-cell wall is ~3-4×
    # the single-agent equivalent. Reuses the same cap × condition matrix.
    ma_tiers = [t.strip() for t in (args.multi_agent_tiers or "").split(",") if t.strip()]
    if ma_tiers:
        print(f"\n=== MULTI-AGENT POST-SUITE: workers={ma_tiers} "
              f"orchestrator={args.multi_agent_orchestrator} ===\n")
        ma_total = len(ma_tiers) * len(capabilities) * len(think_modes) * len(tools_modes)
        ma_done = 0
        ma_started = time.time()
        for worker_tier in ma_tiers:
            # The /v1/chat/completions handler routes via the orchestrator
            # tier's window; warm BOTH orchestrator and worker so the first
            # cell doesn't eat the cold-spawn penalty.
            print(f"\n[multi-agent] warming orchestrator "
                  f"({args.multi_agent_orchestrator})…", flush=True)
            if not _warm_tier_blocking(args.api, args.multi_agent_orchestrator):
                print(f"  FAIL warm orchestrator — skipping multi-agent for {worker_tier}",
                      flush=True)
                continue
            print(f"[multi-agent] warming worker ({worker_tier})…", flush=True)
            if not _warm_tier_blocking(args.api, worker_tier):
                print(f"  FAIL warm worker — skipping multi-agent for {worker_tier}",
                      flush=True)
                continue
            ma_options = {
                "enabled": True,
                "orchestrator_tier": args.multi_agent_orchestrator,
                "worker_tier": worker_tier,
            }
            ma_label = f"multi_{worker_tier}"
            for tools_label in tools_modes:
                for think_label in think_modes:
                    think = {"off": False, "on": True}[think_label]
                    for cap in capabilities:
                        if deadline and time.time() > deadline:
                            break
                        print(f"  [{datetime.now().strftime('%H:%M:%S')}] "
                              f"Running {ma_label} × {cap} × think={think_label} × "
                              f"tools={tools_label}…", flush=True)
                        t0 = time.time()
                        # Orchestrator (versatile, 65k window) dictates the
                        # context window — workers receive sub-prompts. So
                        # use the orchestrator's window for needle filtering.
                        orch_window = TIER_CONTEXT_WINDOW.get(
                            args.multi_agent_orchestrator,
                        )
                        cell = run_cell(
                            args.api, args.multi_agent_orchestrator, cap,
                            _depth_for(worker_tier),
                            max_tokens=args.max_tokens,
                            per_problem_timeout=args.per_problem_timeout * 3,
                            think=think,
                            tools=tools_label,
                            early_stop_margin=early_stop_margin,
                            early_stop_confidence=args.confidence,
                            tier_context_window=orch_window,
                            multi_agent=True,
                            multi_agent_options=ma_options,
                        )
                        # Re-tag the cell so the dashboard shows it as a
                        # distinct multi-agent tier rather than overwriting
                        # the orchestrator's single-agent results.
                        cell.tier = ma_label
                        wall = time.time() - t0
                        ma_done += 1
                        n = max(1, cell.n_problems)
                        p = cell.pass_rate
                        import math as _math
                        z = 1.96; z2 = z * z
                        denom = 1.0 + z2 / n
                        half = (z * _math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))) / denom
                        z_a = 1.96; z_b = 0.84
                        var = max(0.0, p * (1 - p) * 2 / n)
                        mde = (z_a + z_b) * _math.sqrt(var)
                        skipped_note = ""
                        if getattr(cell, "n_skipped_ctx", 0):
                            skipped_note = f" [skipped {cell.n_skipped_ctx} ctx-overflow]"
                        print(f"    [{datetime.now().strftime('%H:%M:%S')}] "
                              f"-> pass {cell.n_passed}/{n} = {p*100:.1f}% "
                              f"(95% CI ±{half*100:.1f}pp, MDE@80% pwr ±{mde*100:.1f}pp) "
                              f"in {wall:.0f}s (mean lat {cell.mean_latency_s:.1f}s) "
                              f"[ma {ma_done}/{ma_total}]{skipped_note}", flush=True)
                        all_results.append(cell)
                        write_json(all_results, cumulative_path)
                        write_markdown(all_results, cumulative_path.with_suffix(".md"))
            print(f"\n=== multi-agent worker {worker_tier} done ===", flush=True)
        ma_wall = time.time() - ma_started
        print(f"\n=== MULTI-AGENT POST-SUITE done in {ma_wall/60:.1f} min "
              f"({ma_done}/{ma_total} cells) ===")
        print(f"  JSON: {cumulative_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
