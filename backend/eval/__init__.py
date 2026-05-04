"""Capability benchmarks for the chat tiers.

Throughput numbers (cold-load + tok/s) live in `scripts/bench_tiers.py`.
This package adds the *capability* axis — does the model actually solve
problems, beyond just answering at speed N. Five capability families:

  - reasoning: AIME 2024 (math olympiad)
  - math:      GSM8K (grade-school word problems)
  - coding:    HumanEval (Python function completion)
  - knowledge: MMLU subset (multiple choice across 57 subjects)
  - long_context: synthetic needle-in-haystack at 4 / 16 / 65 / 131 k

Each grader exposes:
  - load(depth) -> list[Problem]            # subset by depth (fast/medium/full)
  - prompt(problem) -> str                  # how to ask the model
  - score(problem, model_output) -> bool    # exact-match grader

The runner in `runner.py` wires these into the live backend's tier
endpoint and aggregates pass-rate + per-question latency. Output is a
JSON file under `data/eval/results/eval-<timestamp>.json` plus a
markdown summary printed to stdout.

Datasets are vendored under `data/eval/datasets/` so eval works offline.
"""
