# VRAM scheduler

The backend owns one reference-counted registry of loaded models. Every
`POST /v1/chat/completions` enters `scheduler.reserve(tier_id)` before
it calls the backend (Ollama, llama.cpp) and releases on stream
completion.

The scheduler's job is to answer **"can this tier fit in VRAM right
now, and if not, what do I evict?"** — without ever blocking the event
loop.

## State model

| State | Meaning |
|---|---|
| `LOADING` | Another request is loading this tier; newcomers wait on its `load_event` |
| `RESIDENT` | In VRAM, ready; `refcount` tracks in-flight requests |
| `EVICTING` | Marked for unload; new reservations can't use this slot |

Each `LoadedModel` carries:
- `vram_estimate_gb` — from `config/models.yaml`
- `observed_cost_gb` — EMA-smoothed actual post-load delta, persisted to
  `data/vram_observed.json` so restarts don't lose tuning
- `effective_cost()` → `max(estimate, observed)` for eviction math
- `slot_capacity` — max concurrent requests per loaded model, driven by
  `TierConfig.parallel_slots` (and Ollama's `num_parallel`)
- `pinned` — pinned tiers (vision, optionally orchestrator) are never
  eviction candidates

## Eviction policy

`config/vram.yaml::eviction`:

```yaml
eviction:
  policy: lru                 # only policy today
  min_residency_sec: 30       # protect freshly-loaded tiers
  pin_orchestrator: false     # orchestrator tier is a regular eviction candidate
  pin_vision: true            # vision can't unload without a restart
```

The picker builds candidates with `state == RESIDENT`, `refcount == 0`,
`not pinned`, sorted by `last_used` ascending, and evicts until the
projected free-VRAM value satisfies the tier we're trying to reserve
(plus the configured headroom).

Minimum residency: a tier that just finished loading isn't evicted for
`min_residency_sec` so flapping reserves don't thrash the GPU.

## Headroom

```yaml
total_vram_gb: 24            # your card
headroom_gb: 1.5             # reserved for CUDA contexts, graphics
poll_interval_sec: 5         # sweeper cadence
```

The sweeper polls actual free VRAM (via pynvml) on `poll_interval_sec`;
if free VRAM drops below `headroom_gb`, it evicts idle tiers preemptively.
Useful when an external GPU consumer (another desktop app) grabs VRAM
without the scheduler's knowledge.

## Slot cap + wait queue

Each resident tier has `slot_capacity` (configured via `parallel_slots`).
Requests above the cap enter the per-tier wait queue
(`asyncio.Condition`):

- `max_depth_per_tier` (default 10) — queue overflow returns HTTP 503
  with `QueueFull`
- `max_wait_sec` (default 60) — timeout surfaces as `QueueTimeout` in
  the SSE stream
- `position_update_interval_sec` — how often the scheduler emits queue
  position progress to the caller

The frontend renders queue position via the `agent.queue` SSE event so
users see "Waiting for a free slot — position 3."

## Multi-agent VRAM handling

For a multi-agent run, the orchestrator releases its tier before
spawning workers so worker models can load:

```yaml
multi_agent:
  release_orchestrator_during_workers: true
  synthesis_reload_timeout_sec: 60
```

When the workers finish and synthesis begins, the orchestrator re-reserves.
If the orchestrator can't reload within `synthesis_reload_timeout_sec`
the run returns an error rather than hanging.

## Observability

`GET /api/vram` returns:

```json
{
  "total_vram_gb": 24.0,
  "free_vram_gb_actual": 14.3,
  "free_vram_gb_projected": 6.5,
  "loaded": [
    {"tier_id": "versatile", "state": "resident", "refcount": 2, "slot_capacity": 2,
     "effective_cost_gb": 18.0, "last_used": 1714050123.4}
  ]
}
```

`projected` = `total − sum(effective_cost)` for loaded tiers; `actual` is
the pynvml read. Divergence hints at external consumers.

Loaded-tier names also surface in the chat's bottom-right telemetry pill
via `GET /api/system`.

## Tuning knobs

| Knob | Effect |
|---|---|
| `OLLAMA_KEEP_ALIVE` | Ollama's own unload timer, keep < backend eviction for snappy eviction |
| `OLLAMA_NUM_PARALLEL` | Matches `parallel_slots`; more slots = higher KV footprint |
| `OLLAMA_FLASH_ATTENTION` | Flash Attention 2 — faster prefill, lower KV VRAM |
| `vram.observed_costs.learning_rate` | EMA mixing factor for the observed-cost update |
| `vram.observed_costs.persist_path` | Where observed costs live on disk |

## When to edit `model_tag`

When the Ollama library on-disk doesn't match `config/models.yaml`:

1. `ollama pull <new tag>`
2. Update `tiers.<tier_id>.model_tag` in `config/models.yaml`
3. `curl -X POST localhost:8000/admin/reload` (admin-gated) or restart
   the backend

The scheduler evicts the old entry automatically if the tag changed.
