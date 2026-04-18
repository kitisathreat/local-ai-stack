# Setup Progress

## Config
- Admin email: kitisathreat@gmail.com
- Admin password: saved in .env.local
- Discord webhook: configured in .claude/settings.json (Notification + Stop hooks)

## Phase 0 — Notifications
- [x] Discord webhook configured
- [x] Notification hooks active (Claude Code → Discord on wait/finish)

## Phase 1 — Docker
- [x] WSL features enabled (Microsoft-Windows-Subsystem-Linux + VirtualMachinePlatform)
- [x] Reboot done
- [x] Docker Desktop 4.68.0 installed via winget
- [x] Docker verified running

## Phase 2 — Open WebUI
- [x] Container running (open-webui, port 3000)
- [x] HTTP 200 from localhost:3000
- [x] Started with env vars: RAG_EMBEDDING_ENGINE=openai, OPENAI_API_BASE_URL=http://host.docker.internal:1234/v1

## Phase 3 — LM Studio
- [x] LM Studio server started (port 1234, CORS + network enabled)
- [x] Model loaded: deepseek/deepseek-r1-0528-qwen3-8b
- [x] Open WebUI admin account created (kitisathreat@gmail.com, role: admin)
- [x] LM Studio wired into Open WebUI (all 8 models visible via /api/models)

## Phase 4 — Tailscale
- [x] Installed (Tailscale 1.96.3)
- [x] Authenticated (kitisathreat@ account)
- PC Tailscale IP: 100.65.252.37

## Phase 5 — Custom Model
- [x] Kit's Assistant model created
- Base model: deepseek/deepseek-r1-0528-qwen3-8b
- System prompt: technical assistant with PC hardware / GPU / local AI expertise

## Phase 6 — Context Management
- [x] Context length set to 8192 (num_ctx on Kit's Assistant model)
- [x] Memory: /api/v1/memories/ endpoint accessible and ready — enable per-conversation in chat UI (click memory icon)

---

## === SETUP COMPLETE ===

| | |
|---|---|
| Open WebUI (local) | http://localhost:3000 |
| Open WebUI (remote via Tailscale) | http://100.65.252.37:3000 |
| Admin email | kitisathreat@gmail.com |
| Admin password | in .env.local |
| Custom model | Kit's Assistant |
| Base LLM | deepseek/deepseek-r1-0528-qwen3-8b |
| Context window | 8192 tokens |
| LM Studio port | 1234 |
| Tailscale IP | 100.65.252.37 |

## Manual steps remaining
- Install Tailscale on your phone, sign in with the same account (kitisathreat@gmail.com), then visit http://100.65.252.37:3000
- To use memory in a conversation: click the memory/brain icon in the chat input area to enable it for that session
