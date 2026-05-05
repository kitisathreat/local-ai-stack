"""Run a small N-problem bench cell against a tier with explicit
think setting. Used to validate fixes before rerunning the full bench."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.eval.runner import run_cell


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--api", default="http://127.0.0.1:18000")
    p.add_argument("--tier", required=True)
    p.add_argument("--capability", required=True,
                   choices=("knowledge", "math", "reasoning", "coding", "long_context"))
    p.add_argument("--n", type=int, default=10)
    p.add_argument("--think", choices=("on", "off"), required=True)
    p.add_argument("--max-tokens", type=int, default=4096)
    p.add_argument("--per-problem-timeout", type=int, default=180)
    args = p.parse_args()

    # Monkey-patch the dataset loader to take only first N problems
    import backend.eval.runner as runner
    orig = runner.CAPABILITIES[args.capability]
    runner.CAPABILITIES[args.capability] = lambda d: orig(d)[:args.n]

    cell = run_cell(
        args.api, args.tier, args.capability, "fast",
        max_tokens=args.max_tokens,
        per_problem_timeout=args.per_problem_timeout,
        think={"on": True, "off": False}[args.think],
    )
    print()
    print(f"== {args.tier} × {args.capability} × think={args.think}: "
          f"{cell.n_passed}/{cell.n_problems} = {cell.pass_rate*100:.1f}%")
    print(f"   wall: {cell.wall_seconds:.0f}s, mean lat: {cell.mean_latency_s:.1f}s")
    fails = [p for p in cell.problems if not p.passed]
    print(f"\n-- failure previews ({len(fails)}) --")
    for f in fails[:5]:
        print(f"  [{f.id}] tok={f.output_tokens} text_len={f.output_text_len} err={f.error}")
        print(f"    preview: {f.output_preview[:160]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
