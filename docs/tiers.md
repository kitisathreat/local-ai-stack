# Tiers — model routing, reasoning, and slash commands

The backend exposes a **5-tier model roster** behind one OpenAI-compatible
`/v1/chat/completions` endpoint. The router picks a tier per request based
on explicit hints (slash commands, attachments) and heuristics (message
length, code-block detection, question-mark count).

Canonical tier names: `fast`, `versatile`, `highest_quality`, `coding`,
`vision`. They're configured in `config/models.yaml`; aliases like
`balanced` → `versatile` are resolved via the `aliases:` table so older
references keep working.

## The tiers

| Tier | Typical model | VRAM | Strength | When the router picks it |
|---|---|---|---|---|
| `fast` | Qwen3.5 9B or similar 7–9B | ~6 GB | Snappy replies, short messages, casual chat | Short prompts, no code, no reasoning triggers |
| `versatile` | Qwen3.6 35B (MoE A3B) | ~18 GB | Default all-rounder, also the orchestrator tier | Everything that doesn't hit a specialist signal |
| `highest_quality` | Qwen3 72B Q3_K_M | ~34 GB | Longform reasoning, nuanced analysis | Dense prose, structured thinking, multi-step reasoning |
| `coding` | Qwen3-Next-80B-A3B-Thinking | ~28 GB | Code generation, debugging | Prompt contains code blocks, `implement`, `refactor`, etc. |
| `vision` | Qwen3.6 35B + mmproj (llama.cpp) | ~22 GB | Multimodal, OCR, chart reading | Any message carrying `image_url` parts |

The exact model tags ship in `config/models.yaml` — swap them to match what
you've pulled locally. The `model_tag` for Ollama tiers must exist in
`ollama list`; for the vision tier, drop the GGUF + mmproj into `./models/`
(see `scripts/setup-models.sh`).

## Reasoning toggle

Tiers that support Qwen3's "thinking" mode accept a `think` flag:

- `think: true` — the model does a chain-of-thought pass inside
  `<think>…</think>` before the visible answer. Higher quality, more tokens.
- `think: false` — direct answer. Fast tier defaults to this.
- `think: null` (auto) — the router decides based on message shape
  (regexes in `config/router.yaml::auto_thinking_signals`).

The frontend reasoning picker sends `"auto" | "on" | "off"`. Per-user
overrides live in `/preferences` (see `docs/auth.md`).

## Slash commands

Typed as the first word of a message. All case-insensitive.

| Command | Effect |
|---|---|
| `/tier <name>` | Force a specific tier for this turn (`/tier coding`) |
| `/think` | Toggle thinking on for this turn |
| `/nothink` | Toggle thinking off for this turn |
| `/solo` | Skip multi-agent orchestration even if triggers fire |
| `/swarm` | Force multi-agent orchestration (plan + parallel workers + synthesis) |
| `/forget` | Exclude this chat from memory distillation + encrypted history |

## Multi-agent orchestration

Triggered automatically when the user prompt contains ≥N question marks,
exceeds an estimated output-token threshold, or matches the regexes in
`config/router.yaml::multi_agent.trigger_when_any`. The orchestrator:

1. Reserves the orchestrator tier (default `versatile`) and asks for a
   JSON plan of 2–5 parallel subtasks.
2. Releases the orchestrator, runs all subtasks in parallel on the worker
   tier (default `fast`). Each subtask can be tagged for a specialist
   tier (`CODING`, `VISION`, `REASONING`) which overrides the default
   worker tier.
3. Optional collaborative rounds: after the first pass, workers see
   peer drafts and refine for `interaction_rounds` rounds.
4. Re-reserves the orchestrator for synthesis; the final answer streams
   to the client.

Workers inherit the tool registry (#14), so they can call tools inside
subtasks. Both Ollama and llama.cpp workers support tools (#23).

## VRAM scheduling

See `docs/vram.md` for the full story. Short version: the tier router
hands the chosen tier to the scheduler, which reference-counts loaded
models and evicts the LRU unpinned tier when VRAM pressure hits the
configured headroom.
