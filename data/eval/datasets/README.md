# Vendored eval datasets

Public capability benchmarks shipped in the repo so `scripts/eval_tiers.py`
runs fully offline. Pulled once via `huggingface_hub.hf_hub_download`
and committed in their original parquet format. Total ~640 KB.

| dataset | path | rows | source | license |
|---|---|---:|---|---|
| HumanEval | `humaneval/HumanEval.parquet` | 164 | `openai/openai_humaneval` | MIT |
| GSM8K | `gsm8k/test.parquet` | 1319 | `openai/gsm8k` (config: main, split: test) | MIT |
| AIME 2024 | `aime2024/aime2024.parquet` | 30 | `Maxwell-Jia/AIME_2024` | unspecified (problems published by MAA) |
| MMLU subset | `mmlu_subset/mmlu_subset.parquet` | 399 | `cais/mmlu` (config: all, split: test) | MIT |

The MMLU subset is a stratified random sample (`random.Random(42)`,
7 questions per subject across all 57 subjects). The full MMLU is
~14 k questions / ~5 MB; the 399-question sample is enough for trend
detection during fast benches without paying the wall-clock cost of
the full eval. The full split can be added later under
`mmlu_full/test.parquet` if needed.

To refresh any dataset, re-run the snippet at the top of the corresponding
grader in `backend/eval/`. Snapshots are deterministic given the same
HF revision (revision pin TODO once the eval framework stabilises).
