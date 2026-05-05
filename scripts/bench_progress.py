"""Render a progress dashboard for the running full bench.

Reads `data/logs/eval-20260504.log`, pieces together per-cell state
since the last `observability installed` line, computes ETA at each
nesting level (cell, condition, overall), and prints a markdown-friendly
block suitable for a Monitor heartbeat.

Usage:
    python scripts/bench_progress.py
"""
from __future__ import annotations

import datetime as _dt
import re
import sys
import time
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def _latest_eval_log() -> Path:
    """Return the most recently modified eval log. The eval logger handle
    is opened with today's date when the bench starts; if a run begins
    before midnight and continues past it, the file path stays the same
    (rotated by content, not name). Naïvely keying on today() loses the
    pre-midnight log. Fall back to the newest eval-*.log on disk."""
    logs_dir = REPO / "data" / "logs"
    today = logs_dir / f"eval-{_dt.date.today():%Y%m%d}.log"
    if today.exists():
        return today
    matches = sorted(logs_dir.glob("eval-2*.log"), key=lambda p: p.stat().st_mtime,
                     reverse=True)
    return matches[0] if matches else today


LOG = _latest_eval_log()


# Mirror of TIER_DEPTH in run_full_bench.py — used for problems-per-cell math.
TIER_DEPTH = {
    "swarm": "fast", "fast": "fast", "fast_r1_distill": "fast", "fast_phi4": "fast",
    "versatile": "medium", "coding": "medium",
    "highest_quality": "full", "reasoning_max": "full",
    "reasoning_xl": "full", "frontier": "full",
}

# Mirror of _DEPTHS in datasets.py — n problems per (capability, depth).
N_PROBLEMS = {
    ("knowledge",            "fast"):  50, ("knowledge",            "medium"): 150, ("knowledge",            "full"):  399,
    ("knowledge_specialized","fast"): 100, ("knowledge_specialized","medium"): 300, ("knowledge_specialized","full"): 12032,
    ("math",                 "fast"):  50, ("math",                 "medium"): 200, ("math",                 "full"): 1319,
    ("math_competition",     "fast"):  50, ("math_competition",     "medium"): 150, ("math_competition",     "full"):  367,
    ("reasoning",            "fast"):  15, ("reasoning",            "medium"):  30, ("reasoning",            "full"):   30,
    ("coding",               "fast"):  30, ("coding",               "medium"):  80, ("coding",               "full"):  164,
    ("coding_basic",         "fast"):  30, ("coding_basic",         "medium"): 100, ("coding_basic",         "full"):  257,
    ("intent",               "fast"):  50, ("intent",               "medium"): 200, ("intent",               "full"):  541,
    ("clarity",              "fast"):  30, ("clarity",              "medium"):  60, ("clarity",              "full"):   80,
    ("long_context",         "fast"):   4, ("long_context",         "medium"):   8, ("long_context",         "full"):   16,
}


CAPABILITIES_ORDER = [
    "knowledge", "knowledge_specialized", "math", "math_competition",
    "reasoning", "coding", "coding_basic", "intent", "clarity", "long_context",
]
TIERS_ORDER = ["swarm", "fast", "versatile", "coding", "highest_quality", "reasoning_max"]
THINK_MODES = ["off", "on"]
TOOLS_MODES = ["off", "auto"]


def _bar(frac: float, width: int = 28) -> str:
    frac = max(0.0, min(1.0, frac))
    fill = int(round(frac * width))
    return "█" * fill + "░" * (width - fill)


def _fmt_eta(sec: float) -> str:
    if sec <= 0 or sec != sec:
        return "—"
    if sec < 90:
        return f"{int(sec)}s"
    if sec < 3600:
        return f"{int(sec/60)}m"
    if sec < 86400:
        return f"{sec/3600:.1f}h"
    return f"{sec/86400:.1f}d"


def parse_log(path: Path) -> dict:
    if not path.exists():
        return {"cells": [], "current": None, "run_start": None}
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    # Find last observability install — that's the current run's start.
    start_idx = 0
    for i, ln in enumerate(lines):
        if "observability installed: component=eval" in ln:
            start_idx = i
    cells: list[dict] = []
    current = None
    run_start_ts = None
    cell_re = re.compile(
        r"eval-cell start tier=(\S+) capability=(\S+) depth=(\S+) n=(\d+) think=(\S+)(?: tools=(\S+))?"
    )
    prob_re = re.compile(r"runner\.py:\d+\s+(\d{3})/(\d+)\s+\S+\s+tok=\d+\s+in\s+([\d.]+)s\s+(PASS|fail)")
    err_re = re.compile(r"runner\.py:\d+\s+(\d{3})/(\d+)\s+\S+\s+ERROR")
    ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    for ln in lines[start_idx:]:
        m_ts = ts_re.match(ln)
        ts = None
        if m_ts:
            try:
                ts = _dt.datetime.strptime(m_ts.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                pass
        m = cell_re.search(ln)
        if m:
            if current and ts:
                current["last_ts"] = ts
            current = {
                "tier": m.group(1),
                "capability": m.group(2),
                "depth": m.group(3),
                "n_total": int(m.group(4)),
                "think": m.group(5).lower() in ("true", "on"),
                "tools": (m.group(6) or "off"),
                "started_ts": ts,
                "last_ts": ts,
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "last_problem": 0,
                "wall_so_far": 0.0,
            }
            cells.append(current)
            if run_start_ts is None and ts:
                run_start_ts = ts
            continue
        if current is None:
            continue
        m = prob_re.search(ln)
        if m:
            n = int(m.group(1))
            wall = float(m.group(3))
            verdict = m.group(4)
            if verdict == "PASS":
                current["passed"] += 1
            else:
                current["failed"] += 1
            current["last_problem"] = n
            current["wall_so_far"] += wall
            if ts:
                current["last_ts"] = ts
            continue
        m = err_re.search(ln)
        if m:
            current["errors"] += 1
            n = int(m.group(1))
            current["last_problem"] = n
            if ts:
                current["last_ts"] = ts
    return {"cells": cells, "current": current, "run_start": run_start_ts}


def _shade(pct: float) -> str:
    """4-char cell colored by pass rate. Uses Unicode block density to
    encode the value so a quick visual scan tells you good vs bad cells
    even before reading the number."""
    if pct != pct:  # NaN
        return " — "
    if pct < 0:
        return " ?  "
    if pct >= 95:
        return "████"
    if pct >= 80:
        return "▓▓▓ "
    if pct >= 65:
        return "▓▓░ "
    if pct >= 50:
        return "▓░░ "
    if pct >= 35:
        return "░░░ "
    if pct >= 20:
        return "·░  "
    return "·   "


def _shade_compact(pct: float) -> str:
    """1-char shaded glyph for tight grids."""
    if pct != pct:
        return "—"
    if pct < 0:
        return "·"
    if pct >= 90:
        return "█"
    if pct >= 75:
        return "▓"
    if pct >= 60:
        return "▒"
    if pct >= 40:
        return "░"
    return "·"


def _build_grid(cells: list[dict]) -> dict:
    """Returns nested dict: grid[tier][capability][(think_str,tools_str)] = pct.
    Only includes cells that have any results (passed+failed > 0)."""
    grid: dict = {}
    for c in cells:
        n = c["passed"] + c["failed"] + c["errors"]
        if n == 0:
            continue
        rate = 100.0 * c["passed"] / n
        key = ("on" if c["think"] else "off", c["tools"])
        grid.setdefault(c["tier"], {}).setdefault(c["capability"], {})[key] = rate
    return grid


def render(state: dict) -> str:
    cells = state["cells"]
    current = state["current"]
    run_start = state["run_start"]
    now = time.time()

    # Total cells across the planned bench: 6 tiers × 10 caps × 2 think × 2 tools = 240
    total_cells = len(TIERS_ORDER) * len(CAPABILITIES_ORDER) * len(THINK_MODES) * len(TOOLS_MODES)
    cells_per_condition = len(TIERS_ORDER) * len(CAPABILITIES_ORDER)
    n_conditions = len(THINK_MODES) * len(TOOLS_MODES)

    # Completed cells = those where last_problem == n_total OR a later cell started
    completed = []
    for i, c in enumerate(cells):
        is_last = (i == len(cells) - 1)
        finished = (c["passed"] + c["failed"] + c["errors"]) >= c["n_total"]
        if not is_last or finished:
            completed.append(c)

    n_completed = len(completed)
    overall_frac = n_completed / total_cells

    # Wall time per completed cell — use last_ts - started_ts if available
    cell_walls = []
    for c in completed:
        if c.get("started_ts") and c.get("last_ts"):
            cell_walls.append(c["last_ts"] - c["started_ts"])
    median_cell = sorted(cell_walls)[len(cell_walls)//2] if cell_walls else 60
    avg_cell = (sum(cell_walls)/len(cell_walls)) if cell_walls else 60

    overall_eta = (total_cells - n_completed) * max(median_cell, 30)

    # Current condition computation: which (think, tools) is the current cell in?
    cond_idx = 0
    cells_in_current_condition_done = 0
    if current:
        cond_key = (current["think"], current["tools"])
        # Index = tools_idx * len(think_modes) + think_idx (matches run order: outer tools, inner think)
        try:
            t_idx = TOOLS_MODES.index(current["tools"])
            th_idx = THINK_MODES.index("on" if current["think"] else "off")
            cond_idx = t_idx * len(THINK_MODES) + th_idx
        except ValueError:
            pass
        # Cells done in this condition = those whose (think, tools) match this cell
        for c in completed:
            if c["think"] == current["think"] and c["tools"] == current["tools"]:
                cells_in_current_condition_done += 1

    cond_frac = cells_in_current_condition_done / cells_per_condition if cells_per_condition else 0
    cond_eta = (cells_per_condition - cells_in_current_condition_done) * max(median_cell, 30)

    # Current cell progress
    cell_frac = 0.0
    cell_eta = 0
    cell_pass_rate = 0
    if current:
        n_done = current["passed"] + current["failed"] + current["errors"]
        cell_frac = n_done / max(1, current["n_total"])
        if n_done > 0 and current.get("started_ts"):
            elapsed = now - current["started_ts"]
            per_problem = elapsed / n_done
            remaining = current["n_total"] - n_done
            cell_eta = per_problem * remaining
        if (current["passed"] + current["failed"]) > 0:
            cell_pass_rate = 100 * current["passed"] / (current["passed"] + current["failed"])

    out: list[str] = []
    ts_now = _dt.datetime.now().strftime("%H:%M:%S")
    out.append(f"```")
    out.append(f"┌─ FULL BENCH PROGRESS @ {ts_now} ─────────────────────────────────")
    if current:
        cell_label = f"{current['tier']:>15s} × {current['capability']:<22s} think={'on' if current['think'] else 'off'} tools={current['tools']}"
        n_done = current["passed"] + current["failed"] + current["errors"]
        out.append(f"│ Current cell: {cell_label}")
        out.append(f"│   {_bar(cell_frac)} {n_done}/{current['n_total']} ({cell_frac*100:.0f}%)  pass={cell_pass_rate:.0f}%  ETA {_fmt_eta(cell_eta)}")
    else:
        out.append(f"│ (no active cell)")
    cond_label = ""
    if current:
        cond_label = f"think={'on' if current['think'] else 'off'} tools={current['tools']}"
    out.append(f"│ Condition ({cond_label}): {cells_in_current_condition_done}/{cells_per_condition}")
    out.append(f"│   {_bar(cond_frac)} ({cond_frac*100:.0f}%)  ETA {_fmt_eta(cond_eta)}")
    out.append(f"│ Overall: {n_completed}/{total_cells} cells across {n_conditions} conditions")
    out.append(f"│   {_bar(overall_frac)} ({overall_frac*100:.0f}%)  ETA {_fmt_eta(overall_eta)}")
    if cell_walls:
        out.append(f"│ Per-cell wall: median {_fmt_eta(median_cell)}, avg {_fmt_eta(avg_cell)}")
    out.append(f"├─ Pass-rate heatmap (averaged across all conditions) ──────────")
    grid = _build_grid(completed + ([current] if current else []))
    # Compact 1-char-per-condition layout per cell, but show 4 conditions
    # stacked into a single row by averaging. Header row has capability
    # short labels, body row per tier with per-cap glyph + percent.
    cap_short = {
        "knowledge": "knw", "knowledge_specialized": "kSp",
        "math": "mth", "math_competition": "mC",
        "reasoning": "rea", "coding": "cod", "coding_basic": "cB",
        "intent": "int", "clarity": "clr", "long_context": "lc",
    }
    header = "│  " + " " * 16 + "  ".join(f"{cap_short.get(c, c[:3]):>3s}" for c in CAPABILITIES_ORDER)
    out.append(header)
    for tier in TIERS_ORDER:
        if tier not in grid:
            continue
        row_cells = []
        for cap in CAPABILITIES_ORDER:
            cap_d = grid[tier].get(cap, {})
            if not cap_d:
                row_cells.append("  —")
                continue
            avg_pct = sum(cap_d.values()) / len(cap_d)
            glyph = _shade_compact(avg_pct)
            # Show 2-digit % alongside the glyph; 3 chars total to fit
            # under the 3-char header
            row_cells.append(f"{glyph}{int(avg_pct):2d}")
        out.append(f"│  {tier:>15s}  " + "  ".join(row_cells))
    out.append(f"│  legend: █≥90  ▓≥75  ▒≥60  ░≥40  ·<40   —no data")

    out.append(f"├─ Tier averages (pass-rate bars, all caps × all conditions) ───")
    # Per-tier average across all completed cells, rendered as horizontal bar.
    for tier in TIERS_ORDER:
        if tier not in grid:
            continue
        all_rates = []
        for cap in CAPABILITIES_ORDER:
            for v in grid[tier].get(cap, {}).values():
                all_rates.append(v)
        if not all_rates:
            continue
        avg = sum(all_rates) / len(all_rates)
        n_cells = len(all_rates)
        bar_width = 40
        fill = int(round((avg / 100.0) * bar_width))
        bar = "█" * fill + "░" * (bar_width - fill)
        out.append(f"│  {tier:>15s} {bar} {avg:5.1f}%  ({n_cells} cells)")

    # Condition deltas — does thinking help? Do tools help? Aggregate the comparison.
    out.append(f"├─ Condition Δ (avg pass-rate change vs baseline off/off) ──────")
    cond_avg = {}  # (think, tools) -> avg
    cond_n = {}
    for c in completed + ([current] if current else []):
        n = c["passed"] + c["failed"] + c["errors"]
        if n == 0:
            continue
        rate = 100 * c["passed"] / n
        key = ("on" if c["think"] else "off", c["tools"])
        cond_avg.setdefault(key, []).append(rate)
    baseline_key = ("off", "off")
    baseline = sum(cond_avg.get(baseline_key, []))/max(1, len(cond_avg.get(baseline_key, []))) \
               if cond_avg.get(baseline_key) else None
    for k in [("off","off"), ("off","auto"), ("on","off"), ("on","auto")]:
        rates = cond_avg.get(k, [])
        if not rates:
            continue
        avg = sum(rates) / len(rates)
        delta = (avg - baseline) if baseline is not None else 0
        sign = "+" if delta >= 0 else ""
        # delta bar — center at 0, range ±30pp
        center = 20
        delta_clamped = max(-30, min(30, delta))
        offset = int(round(delta_clamped / 30.0 * center))
        bar = list("│" + " " * (center*2 + 1))
        bar[center] = "│"
        # mark the avg position
        pos = center + offset
        bar[pos] = "█" if delta >= 0 else "▒"
        delta_bar = "".join(bar)[1:]  # drop initial │
        out.append(f"│  think={k[0]:<3s} tools={k[1]:<4s}  {avg:5.1f}%  Δ {sign}{delta:+5.1f}pp  {delta_bar}  ({len(rates)} cells)")

    out.append(f"├─ Latest finished cells ────────────────────────────────────────")
    for c in completed[-6:]:
        n_done = c["passed"] + c["failed"] + c["errors"]
        rate = 100 * c["passed"] / max(1, n_done)
        wall = (c["last_ts"] - c["started_ts"]) if (c.get("started_ts") and c.get("last_ts")) else 0
        cond = f"th={'on ' if c['think'] else 'off'} t={c['tools']:<4s}"
        # Inline mini-bar
        bar_w = 12
        fill = int(round((rate / 100.0) * bar_w))
        mini = "█" * fill + "░" * (bar_w - fill)
        out.append(f"│  {cond} {c['tier']:>15s} × {c['capability']:<22s} {mini} {rate:5.1f}%  {_fmt_eta(wall)}")
    out.append(f"└──────────────────────────────────────────────────────────────")
    out.append(f"```")
    return "\n".join(out)


def main() -> int:
    # Windows default stdout encoding is cp1252 which can't render
    # the box-drawing + block-element characters used in the dashboard.
    # Reconfigure stdout to UTF-8 for the duration of this print.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    state = parse_log(LOG)
    print(render(state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
