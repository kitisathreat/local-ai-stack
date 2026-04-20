# Multi-Backend Host Support

The backend can route inference requests to multiple backend hosts — your
local Ollama, a GPU on AWS, a Google Colab notebook tunnelled via
`ngrok` / `cloudflared`, or any OpenAI-compatible proxy (vLLM, TGI,
LiteLLM, Together). Hosts are declared once in `config/hosts.yaml` and
referenced by name from individual tiers in `config/models.yaml`.

## Design at a glance

```
  ChatRequest
     │
     ▼
  Router → picks tier
     │
     ▼
  TierDispatcher
     ├─ tier.host                    (primary)
     ├─ tier.host_fallbacks[...]     (ordered fallback list)
     └─ __legacy_ollama__ / __legacy_llama_cpp__     (always-on floor)
     │
     ▼  (first candidate whose circuit is closed)
  BackendClient → streams tokens back to the user
```

The **legacy clients** — built from `OLLAMA_URL` and `LLAMACPP_URL` env vars —
are never removed. They act as the final fallback when every host registered
in `hosts.yaml` is unhealthy. Per-tier opt-out via
`allow_legacy_fallback: false`, or globally via
`failover.legacy_fallback_enabled: false` in `hosts.yaml`.

## Circuit breaker

Each host has a circuit-breaker state:

- **Closed** (healthy) — requests flow normally.
- **Open** — after `failover.open_after` consecutive failures (default 3),
  the host is skipped by the dispatcher until `half_open_probe_sec`
  (default 30s) has elapsed.
- **Half-open** — the next eligible request is routed to the open host as
  a probe. Success closes the circuit; failure keeps it open and restarts
  the timer.

`GET /admin/hosts` shows the current breaker state. Operators can force-close
a circuit with `POST /admin/hosts/{name}/reset-breaker` when they've just
fixed a remote host.

## Worked example: Google Colab + ngrok

1. In a Colab notebook with a GPU runtime:
   ```bash
   !curl -fsSL https://ollama.com/install.sh | sh
   !ollama pull qwen3.5:9b
   !ollama serve &
   !npm install -g localtunnel
   !lt --port 11434 --subdomain your-lab
   ```

2. In `.env.local`:
   ```bash
   COLAB_OLLAMA_TOKEN=            # leave blank if localtunnel isn't auth-gated
   ```

3. In `config/hosts.yaml`, flip the pre-populated `colab-a100` entry:
   ```yaml
   colab-a100:
     kind: ollama
     url: https://your-lab.loca.lt
     location: remote
     total_vram_gb: 16             # T4 runtime
     enabled: true                 # flipped from false
   ```

4. Point a tier at it in `config/models.yaml`:
   ```yaml
   fast:
     ...
     host: colab-a100
     host_fallbacks: [local-ollama]  # fall back to local if Colab dies
   ```

5. Hot-reload: `POST /admin/reload` (or restart).

6. Confirm in `GET /admin/hosts` that `colab-a100` shows
   `health.circuit_open: false` after its first successful probe.

## Worked example: AWS EC2 + bearer token

EC2 runs Ollama behind a Caddy proxy that requires a bearer token:

```yaml
# config/hosts.yaml
aws-g5:
  kind: ollama
  url: https://ai.example.com
  location: remote
  total_vram_gb: 24
  auth_env: AWS_OLLAMA_TOKEN
  verify_tls: true
  enabled: true
```

```bash
# .env.local
AWS_OLLAMA_TOKEN=sk-aws-ollama-xxxx
```

The backend attaches `Authorization: Bearer sk-aws-ollama-xxxx` to every
request to `ai.example.com`.

## Worked example: OpenAI-compatible proxy

A LiteLLM proxy that fans out to cloud providers:

```yaml
# config/hosts.yaml
litellm-proxy:
  kind: openai
  url: https://llm-proxy.example.com/v1
  location: remote
  total_vram_gb: 999            # unmetered; the proxy handles scheduling
  auth_env: LITELLM_API_KEY
  enabled: true
```

Use this to offload a tier to a hosted model without leaving the local-ai
abstraction — it just looks like another tier to the frontend.

## Backward compatibility

If `config/hosts.yaml` is absent, the backend synthesises two hosts from the
legacy `OLLAMA_URL` / `LLAMACPP_URL` env vars. Existing deployments keep
working without any config changes. The two synthetic
`__legacy_ollama__` / `__legacy_llama_cpp__` entries are added regardless.

## VRAM scheduling

Local hosts (`location: local`) use the existing pynvml-based VRAM
scheduler. Remote hosts advertise a declared `total_vram_gb` but the
scheduler does not gate remote requests on GPU residency today — that's a
planned follow-up (the dispatcher works regardless). Remote hosts are
trusted to manage their own VRAM; the circuit breaker absorbs the
consequences when they can't.
