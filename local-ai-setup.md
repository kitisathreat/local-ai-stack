# Local AI Setup: Open WebUI + LM Studio + Tailscale

## Overview

This guide sets up a self-hosted AI chat interface (Open WebUI) connected to your local LM Studio instance, accessible from any browser or phone via Tailscale. It also covers configuring system prompts (like Claude's "skills") and context management.

**Estimated time:** 1–2 hours  
**Requirements:** Windows 10 or later, LM Studio already installed, admin access on your PC

---

## Phase 1: Install Docker Desktop

1. Go to [https://www.docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop) and download the Windows installer.
2. Run the installer. When prompted, select the **WSL 2** backend.
3. Restart your PC when prompted.
4. Open Docker Desktop and wait for it to fully load (the whale icon in the system tray should stop animating).

---

## Phase 2: Install Open WebUI

1. Open **PowerShell** (search for it in the Start menu — right-click and run as Administrator).
2. Paste and run this command:

```
docker run -d -p 3000:8080 --add-host=host.docker.internal:host-gateway -v open-webui:/app/backend/data --name open-webui --restart always ghcr.io/open-webui/open-webui:main
```

3. Wait about 2–3 minutes for the image to download and start.
4. Open your browser and go to: `http://localhost:3000`
5. Create an admin account — the **first account created** automatically gets admin privileges.

> **Note:** The `--restart always` flag means Open WebUI will launch automatically every time your PC boots, as long as Docker Desktop is running.

---

## Phase 3: Connect LM Studio to Open WebUI

### Step 1 — Enable LM Studio's server

1. Open LM Studio.
2. Click the **Local Server** tab in the left sidebar (icon looks like `<->`).
3. Enable **"Serve on Local Network"**.
4. Enable **"Enable CORS"**.
5. Note the port number — it defaults to **1234**.
6. Load a model and start the server.

### Step 2 — Add LM Studio as a connection in Open WebUI

1. In Open WebUI, click your profile icon (top right) → **Admin Panel**.
2. Go to **Settings → Connections**.
3. Under the **OpenAI API** section, click the **+** (plus) icon.
4. Fill in:
   - **URL:** `http://host.docker.internal:1234/v1`
     - ⚠️ Do not use `localhost` here — Docker can't reach it. Use `host.docker.internal` exactly as written.
   - **API Key:** `lmstudio` (LM Studio doesn't validate this, but the field can't be blank)
5. Click **Verify Connection** — you should see a success message.
6. Click **Save**.
7. Go to a new chat — your loaded models should now appear in the model dropdown at the top.

---

## Phase 4: Install Tailscale (Remote Access)

Tailscale creates a private, encrypted tunnel between your devices so you can access your PC from your phone or from outside your home network — without touching your router.

### Step 1 — Install on your PC

1. Go to [https://tailscale.com/download/windows](https://tailscale.com/download/windows) and download the installer.
2. Run the installer.
3. After installation, a **Tailscale icon** will appear in your system tray (bottom-right, you may need to click the arrow to find it).
4. Right-click the icon → **Log in**.
5. A browser window will open — sign up or log in using a Google or Microsoft account. The free Personal plan supports up to 3 users and is sufficient for personal use.

### Step 2 — Install on your phone

1. Install **Tailscale** from the App Store (iOS) or Google Play (Android).
2. Sign in with the **same account** you used on your PC.

### Step 3 — Connect

1. Go to [https://login.tailscale.com/admin/machines](https://login.tailscale.com/admin/machines) — you should see both your PC and phone listed.
2. Find your **PC's Tailscale IP address** — it will look like `100.x.x.x`.
3. On your phone's browser, navigate to:

```
http://100.x.x.x:3000
```

Replace `100.x.x.x` with your actual Tailscale IP. Open WebUI will load exactly as it does on your PC.

> **Tip:** You can bookmark this address on your phone's home screen and it will behave like an app. This works on cell data, at work, anywhere — Tailscale handles the connection automatically.

---

## Phase 5: Configure System Prompts (Emulating Claude "Skills")

A system prompt is a set of instructions injected at the start of every conversation, invisible to you during chat. This is how you give your local model a consistent personality, expertise, or behavior — similar to how Claude's skills work.

### Creating a custom model with a system prompt

1. In Open WebUI, go to **Workspace → Models** (or Admin Panel → Models).
2. Click **+ New Model**.
3. Fill in:
   - **Name:** Whatever you want to call this persona (e.g., "Kit's Assistant")
   - **Base Model:** Select your LM Studio model from the dropdown
   - **System Prompt:** Write your instructions here. Example:

```
You are a knowledgeable technical assistant. You have expertise in PC hardware, 
GPU modding, local AI infrastructure, and software configuration. 

When answering questions:
- Be precise and specific, not vague
- Think step by step before giving instructions
- If you're unsure, say so rather than guessing
- Prefer concise answers unless the topic requires depth

The user has an intermediate technical background with some engineering coursework.
```

4. Click **Save**.
5. Select this model from the chat dropdown — it will now apply these instructions to every conversation.

> You can create multiple model presets with different system prompts for different use cases (e.g., one for coding help, one for general chat, one for research).

---

## Phase 6: Context Management (Emulating Compacting)

Open WebUI has two built-in tools for managing context:

### Option A — Set a context length limit per model

This caps how many tokens of conversation history get sent with each message, preventing overflow.

1. Go to **Workspace → Models** → click your model → **Advanced Parameters**.
2. Set **Context Length** to a value within your model's limit (e.g., `8192` or `16384`).
3. Save.

When the conversation exceeds this, older messages are automatically trimmed from what gets sent to the model.

### Option B — Enable Memory (persistent facts across chats)

This stores key facts about you and your preferences, injected into future conversations automatically.

1. Go to **Settings → Workspace → Memory**.
2. Enable memory.
3. You can manually add facts (e.g., "I prefer concise answers", "I use Windows 11", "My GPU is RTX Pro 4000 SFF") or let the model extract them from conversation.

> **For full automatic summarization** (where the model summarizes the conversation when the context fills up, then continues): This requires a custom Open WebUI **Function** (a Python plugin). This is an advanced step beyond initial setup. Search the Open WebUI community site at [https://openwebui.com/functions](https://openwebui.com/functions) for "context summarization" — community-built functions can be installed with one click.

---

## Quick Reference

| What you want | Where to go |
|---|---|
| Load a model | LM Studio → Local Server tab → load model |
| Access from PC | `http://localhost:3000` |
| Access from phone | `http://100.x.x.x:3000` (Tailscale IP) |
| Change system prompt | Workspace → Models → your model |
| Add a connection | Admin Panel → Settings → Connections |
| Enable memory | Settings → Workspace → Memory |
| Check Tailscale IPs | https://login.tailscale.com/admin/machines |

---

## Troubleshooting

**Open WebUI shows no models after connecting LM Studio**
- Make sure a model is actually loaded and the server is running in LM Studio
- Confirm "Serve on Local Network" and "Enable CORS" are both on in LM Studio
- Make sure you used `http://host.docker.internal:1234/v1` — not `localhost`

**Can't reach Open WebUI from phone**
- Make sure Tailscale is running and connected on both devices (check the tray icon on PC, app on phone)
- Confirm you're using the `100.x.x.x` IP from Tailscale, not your local network IP
- Make sure Docker Desktop is running on your PC

**Docker command failed or container won't start**
- Open Docker Desktop and check the Containers tab for error logs
- Make sure port 3000 isn't already in use by something else
- Try restarting Docker Desktop and re-running the command

---

## Phase 7: Code Assist Script (Claude Code Integration)

This phase adds `scripts/code_assist.py` — a terminal-based AI coding assistant that connects to your already-running LM Studio and gives you five structured "code message series" modes, parallel multi-agent tasks, automatic model routing, and the ability to run code in Jupyter.

Think of it like having Claude Code's agent loop, but powered by your local models.

### One-time setup

Open a terminal (PowerShell or Command Prompt) and run:

```
pip install openai websocket-client
```

That's the only setup needed. The script reads your `config/models.yaml` automatically.

Also restart your stack once to pick up the Jupyter port change:

```powershell
.\scripts\stop.ps1
.\scripts\start.ps1
```

---

### How to start it

From the project root folder:

```powershell
python scripts/code_assist.py --mode explain
python scripts/code_assist.py --mode review
python scripts/code_assist.py --mode fix
python scripts/code_assist.py --mode test
python scripts/code_assist.py --mode plan
```

You can also pin a specific model profile:

```powershell
python scripts/code_assist.py --mode fix --profile coding
```

Or turn off automatic model routing and always use one model:

```powershell
python scripts/code_assist.py --mode review --profile fixed
```

---

### The 5 modes

Each mode changes how the AI approaches your request. Think of them as different "hats" the assistant puts on.

| Mode | What it does |
|---|---|
| `explain` | Walks through code or concepts in plain English, step by step |
| `review` | Reviews your code for bugs, edge cases, and improvements |
| `fix` | Finds the root cause of a bug and gives you a working fix |
| `test` | Writes Python tests for your code (happy path, edge cases, errors) |
| `plan` | Breaks a task into numbered steps without writing any code |

---

### Built-in commands (type these at the `You>` prompt)

| Command | What it does |
|---|---|
| `/clear` | Wipes the conversation history and starts fresh |
| `/compact` | Summarizes the whole conversation into one short message (saves context window space — use this when the conversation gets long) |
| `/mode review` | Switches to a different mode mid-conversation |
| `/file config/models.yaml` | Loads a file and injects its contents into the conversation so you can ask about it |
| `/multi Write tests and review my parser` | Breaks the task into subtasks and runs them all in parallel, then combines the results |
| `/history` | Shows how many turns are in the current conversation |
| `/exit` | Quits the script |

---

### How automatic model routing works

By default, before answering you, the script asks the fast Qwen 9B model: "How hard is this task?" The answer determines which model handles the real response:

| Difficulty | Model used |
|---|---|
| Easy (simple question) | Qwen 3.5 9B — fast, low GPU load |
| Medium (reasoning needed) | DeepSeek R1 8B — your default quality model |
| Hard (complex code) | Qwen Coder — code-optimized |
| Expert (architecture/research) | Qwen 3.5 35B — most capable, slowest |

The terminal shows you which model was chosen, like:
```
[Task: HARD → using coding model]
```

Use `--profile fixed` to turn this off and always use the same model.

---

### Running code in Jupyter

When the AI writes a code block in its response, you'll see:

```
Run code block in Jupyter? [y/N]:
```

Type `y` and the code runs inside your Jupyter Docker container. The output prints in the terminal, and the AI can see it too — so you can ask follow-up questions like "why did it print that?" or "fix the error above."

---

### Parallel multi-agent tasks

The `/multi` command lets you hand the AI a complex task and have it work on multiple parts at the same time:

```
/multi Write a function to parse models.yaml, write tests for it, and review the code
```

What happens:
1. The AI breaks this into 2-4 subtasks
2. Shows you the list and asks to confirm
3. Runs all subtasks simultaneously (your models are already configured for up to 4 parallel requests)
4. Synthesizes all results into one combined response

---

### Example session

```
python scripts/code_assist.py --mode explain

You> What does a dictionary do in Python?
Assistant: A dictionary is like a real-world lookup table...

You> /mode fix
[Switched to fix mode. History cleared.]

You> /file scripts/start.ps1
[File loaded: scripts/start.ps1 (5842 chars)]

You> The script fails at the Docker check step. Here's the error: ...
Assistant: The root cause is... [provides fix]

Run code block in Jupyter? [y/N]: n

You> /compact
[Compacted: 8 turns → 1 summary]

You> /exit
```

---

### Troubleshooting

**"Missing dependency: run pip install openai"**  
Run `pip install openai websocket-client` in your terminal.

**"Profile 'X' not found in config/models.yaml"**  
Valid profiles are: `fast`, `quality`, `coding`, `large`. Check your spelling.

**Jupyter output says "Could not connect to Jupyter"**  
Make sure you restarted the stack after the `docker-compose.yml` change. Run `.\scripts\start.ps1`.

**Model gives very short or nonsensical answers**  
The context window may be full. Type `/compact` to summarize the conversation and free up space.
