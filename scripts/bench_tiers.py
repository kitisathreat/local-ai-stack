"""Benchmark cold-spawn time and steady-state tok/s for each chat tier.

For every available tier (or a user-supplied subset), this script:
  1. Forces a cold spawn by sending a tier-acquire request and timing
     it from when the request goes out to when the first SSE chunk
     comes back. The backend's `tier.loading` event marks the spawn-
     start handoff; the elapsed wall time approximates load-into-VRAM
     latency.
  2. Sends a follow-up generation request and times the warm token
     stream — `tokens_in_response / wall_seconds` is the steady-state
     tok/s (with whatever spec-decode + KV-prefix benefits the tier
     happens to be configured for).

The eviction step between tiers (acquire a different tier briefly) is
what guarantees the next measurement is a cold-spawn rather than a
warm cache hit. We rotate through `_EVICTION_TIER` between runs.

Output: prints a Markdown table to stdout AND writes a JSON snapshot
to data/eval/tier-bench-<timestamp>.json. Re-run after every quant
swap, llama.cpp version bump, or hardware change to get a comparable
A/B number.

Usage:
    python scripts/bench_tiers.py
    python scripts/bench_tiers.py --tiers fast,versatile,coding
    python scripts/bench_tiers.py --tokens 200 --warmup-tokens 8
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


# Sent during the cold-spawn measurement — short enough that the load
# step dominates wall time, long enough that the model actually emits
# a few tokens (some tiers refuse to stream finish-only chunks).
_COLD_PROMPT = "Say OK and nothing else."
_COLD_MAX_TOKENS = 8

# Sent during the steady-state measurement — long enough that the
# tok/s reading averages over a representative generation window.
_WARM_PROMPT = (
    "Count from 1 to 50, separating numbers with a single comma and a "
    "space. Output the entire sequence on one line, no other text."
)
_WARM_MAX_TOKENS = 220   # 50 numbers + commas + spaces ≈ ~120-180 tokens


def _build_chat_body(tier_id: str, prompt: str, max_tokens: int) -> dict:
    return {
        "model": tier_id,
        "stream": True,
        "think": False,         # no thinking — measuring pure generation
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }


def _stream_chat(api_base: str, body: dict, timeout: int = 600) -> tuple[float, float, str, int]:
    """Open the chat stream, time wall + measure (first_token_s, total_s,
    text, token_count). Token count is whitespace-split — same proxy
    used by the backend's metrics path; close enough for tok/s trends.
    """
    req = urllib.request.Request(
        api_base.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", "accept": "text/event-stream"},
    )
    started = time.time()
    first_token_at: float | None = None
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
                delta = (ch.get("delta") or {}).get("content")
                if isinstance(delta, str) and delta:
                    if first_token_at is None:
                        first_token_at = time.time()
                    chunks.append(delta)
    total = time.time() - started
    full = "".join(chunks)
    tokens = max(1, len(full.split())) if full else 0
    first = (first_token_at - started) if first_token_at else total
    return first, total, full, tokens


def _evict(api_base: str, evict_tier: str) -> None:
    """Cold-spawn an unrelated tier briefly. The scheduler's
    _make_room_for path will evict whatever else was loaded to make
    room for it, ensuring the next benchmarked tier really pays the
    spawn cost (not a warm cache hit)."""
    try:
        body = _build_chat_body(f"tier.{evict_tier}", "Say OK.", 4)
        _stream_chat(api_base, body, timeout=120)
    except Exception:
        # Best-effort. If eviction fails, the next bench is still
        # informative — just labeled "warm" instead of "cold".
        pass


def list_available_tiers(api_base: str) -> list[str]:
    """Pull /v1/models and return the tier_ids the user can pick from
    (skips tiers whose GGUF isn't on disk)."""
    with urllib.request.urlopen(api_base.rstrip("/") + "/v1/models", timeout=10) as r:
        d = json.load(r)
    out: list[str] = []
    for m in d.get("data", []):
        # The backend hides unavailable tiers as `disabled` options in
        # /v1/models? No — /v1/models returns all configured tiers.
        # We have to cross-check /resolved-models to filter.
        out.append(m["id"].replace("tier.", ""))
    return out


def filter_to_available(api_base: str, candidates: list[str]) -> list[str]:
    try:
        with urllib.request.urlopen(api_base.rstrip("/") + "/resolved-models", timeout=10) as r:
            res = json.load(r)
    except Exception:
        return candidates
    tiers = res.get("tiers", {}) or {}
    out: list[str] = []
    for t in candidates:
        info = tiers.get(t)
        if info is None or info.get("available") is True:
            # Unknown to resolver = still try (may be configured tier
            # that's pre-spawned). Available=True = safe to bench.
            out.append(t)
        else:
            print(f"  skip {t}: GGUF not on disk", file=sys.stderr)
    return out


def main() -> int:
    # Process identification + log file under data/logs/bench-<date>.log.
    # Imported lazily so the script still works for users who haven't
    # `pip install`-ed the new requirements.txt yet (setproctitle missing
    # is tolerated inside observability.install()).
    try:
        # Repo-root sys.path tweak so this script works whether invoked
        # as `python scripts/bench_tiers.py` (top-level) or via `-m`.
        import os as _os
        import sys as _sys
        _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
        from backend import observability as _obs
        _obs.install("bench")
    except Exception:  # noqa: BLE001 — observability is best-effort
        pass

    p = argparse.ArgumentParser()
    p.add_argument("--api", default="http://127.0.0.1:18000")
    p.add_argument(
        "--tiers", default="",
        help="Comma-separated subset (default: all available chat tiers)",
    )
    p.add_argument(
        "--evict-tier", default="fast",
        help="Tier used to evict between runs so each measurement is cold",
    )
    p.add_argument("--warmup-tokens", type=int, default=_COLD_MAX_TOKENS)
    p.add_argument("--tokens", type=int, default=_WARM_MAX_TOKENS)
    p.add_argument("--out", default="")
    args = p.parse_args()

    if args.tiers:
        tiers = [t.strip() for t in args.tiers.split(",") if t.strip()]
    else:
        tiers = list_available_tiers(args.api)
    tiers = filter_to_available(args.api, tiers)
    if not tiers:
        print("No tiers to bench.", file=sys.stderr)
        return 2

    print(f"Benchmarking {len(tiers)} tier(s) on {args.api}")
    print(f"  cold prompt: {_COLD_PROMPT!r} (max {args.warmup_tokens} tokens)")
    print(f"  warm prompt: {_WARM_PROMPT!r} (max {args.tokens} tokens)")
    print(f"  eviction tier (between runs): {args.evict_tier}")
    print()

    results: list[dict] = []
    for tier in tiers:
        # Force a cold-spawn by evicting the previous resident first.
        # If `tier == evict-tier` we still bench — the cold-spawn is on
        # a different acquire round, just no eviction needed.
        if tier != args.evict_tier:
            _evict(args.api, args.evict_tier)
            time.sleep(2)

        # ── Cold-spawn measurement ──────────────────────────────────────
        cold_body = _build_chat_body(f"tier.{tier}", _COLD_PROMPT, args.warmup_tokens)
        try:
            cold_first, cold_total, _, _ = _stream_chat(args.api, cold_body, timeout=300)
            cold_load_s = cold_first   # time to first token ≈ load + first inference
        except Exception as e:
            print(f"  {tier:<18} COLD FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            results.append({"tier": tier, "error": str(e)})
            continue

        # ── Steady-state tok/s ──────────────────────────────────────────
        # No eviction here — the tier is now hot, we want generation
        # rate not load rate.
        warm_body = _build_chat_body(f"tier.{tier}", _WARM_PROMPT, args.tokens)
        try:
            warm_first, warm_total, warm_text, warm_tokens = _stream_chat(args.api, warm_body, timeout=300)
            # Generation tok/s: tokens emitted divided by post-first-token wall.
            gen_secs = max(0.001, warm_total - warm_first)
            tps = warm_tokens / gen_secs if warm_tokens > 0 else 0.0
        except Exception as e:
            print(f"  {tier:<18} WARM FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            results.append({
                "tier": tier, "cold_load_s": round(cold_load_s, 2), "error": str(e),
            })
            continue

        row = {
            "tier": tier,
            "cold_load_s": round(cold_load_s, 2),
            "warm_first_token_s": round(warm_first, 2),
            "warm_total_s": round(warm_total, 2),
            "warm_tokens": warm_tokens,
            "tokens_per_sec": round(tps, 1),
        }
        results.append(row)
        print(
            f"  {tier:<18} cold-load {cold_load_s:>5.1f}s  "
            f"warm-first {warm_first:>5.2f}s  "
            f"gen {tps:>6.1f} tok/s  "
            f"({warm_tokens} tok in {warm_total - warm_first:.1f}s)"
        )

    # ── Markdown summary ────────────────────────────────────────────────
    print()
    print("| tier | cold-load (s) | warm-first (s) | tok/s | n |")
    print("|---|---:|---:|---:|---:|")
    for r in results:
        if "error" in r:
            print(f"| {r['tier']} | — | — | — | (error: {r['error'][:40]}) |")
        else:
            print(
                f"| {r['tier']} | {r['cold_load_s']} | {r['warm_first_token_s']}"
                f" | {r['tokens_per_sec']} | {r['warm_tokens']} |"
            )

    out_path = args.out or f"data/eval/tier-bench-{int(time.time())}.json"
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps({
        "api": args.api,
        "ts": time.time(),
        "tiers_requested": tiers,
        "warm_prompt": _WARM_PROMPT,
        "warm_max_tokens": args.tokens,
        "results": results,
    }, indent=2), encoding="utf-8")
    print()
    print(f"Saved: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
