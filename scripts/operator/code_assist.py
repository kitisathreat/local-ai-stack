"""
code_assist.py — Local AI coding assistant for Kit's AI stack.

Connects to LM Studio (already running at http://localhost:1234/v1) and starts
an interactive coding session. Supports five modes, parallel multi-agent tasks,
automatic task difficulty routing, conversation compaction, and Jupyter execution.

Usage:
    python scripts/operator/code_assist.py --mode explain
    python scripts/operator/code_assist.py --mode review --profile coding
    python scripts/operator/code_assist.py --help

Install dependencies first:
    pip install openai websocket-client
"""

import argparse
import json
import os
import re
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Parse args early so --help always works even without dependencies installed
_parser = argparse.ArgumentParser(
    description="Kit's local AI coding assistant — connects to LM Studio",
    add_help=True
)
_parser.add_argument("--mode", choices=["explain","review","fix","test","plan"], default="explain",
    help="Conversation mode: explain, review, fix, test, plan (default: explain)")
_parser.add_argument(
    "--profile",
    choices=["fast","versatile","highest_quality","coding","vision",
             "fixed","quality","large","balanced","analyst","creative","roleplay","summarizer"],
    default="",
    help="Tier from config/models.yaml (new 5-tier schema; legacy 9-profile names are accepted as aliases). Use 'fixed' to disable auto-routing.",
)
if "--help" in sys.argv or "-h" in sys.argv:
    _parser.print_help()
    sys.exit(0)

try:
    from openai import OpenAI
except ImportError:
    sys.exit("Missing dependency: run  pip install openai")

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
REPO_ROOT   = SCRIPT_DIR.parent
PROMPTS_DIR = SCRIPT_DIR / "prompts"
MODELS_YAML = REPO_ROOT / "config" / "models.yaml"

LMSTUDIO_URL = "http://localhost:1234/v1"
JUPYTER_URL  = "http://localhost:8888"
JUPYTER_TOKEN = "local-ai-stack-token"

VALID_MODES    = ["explain", "review", "fix", "test", "plan"]
# New 5-tier schema — code_assist reads `tiers:` and `model_tag:` from models.yaml.
VALID_PROFILES = ["fast", "versatile", "highest_quality", "coding", "vision"]

# Legacy 9-profile names map to the new tiers (matches config/models.yaml aliases).
LEGACY_ALIASES = {
    "quality": "highest_quality",
    "large": "highest_quality",
    "balanced": "versatile",
    "analyst": "versatile",
    "creative": "fast",
    "roleplay": "fast",
    "summarizer": "fast",
}

# Difficulty → tier routing.
DIFFICULTY_MAP = {
    "EASY": "fast",
    "MEDIUM": "versatile",
    "HARD": "coding",
    "EXPERT": "highest_quality",
}

# Keywords that signal a particular specialization need
VISION_KEYWORDS  = ["image", "photo", "picture", "screenshot", "diagram", "chart", "visual",
                    "look at", "see this", "describe this", "ocr", "pixels", "render"]
MATH_KEYWORDS    = ["math", "equation", "formula", "calculate", "integral", "derivative",
                    "probability", "statistics", "algebra", "geometry", "proof", "theorem",
                    "matrix", "vector", "eigenvalue", "calculus"]
CODE_KEYWORDS    = ["function", "class", "algorithm", "code", "script", "implement", "debug",
                    "compile", "syntax", "api", "library", "module", "import", "refactor"]

# LM Studio vision model name fragments (checked against model IDs at runtime)
VISION_MODEL_HINTS = ["llava", "vision", "-vl", "moondream", "bakllava", "minicpm-v",
                      "phi-3-vision", "qwen-vl", "cogvlm", "internvl", "deepseekvl"]

# ANSI colours for terminal output
CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
DIM    = "\033[2m"
RESET  = "\033[0m"


# ── models.yaml helpers (Phase 6: reads new 5-tier schema) ───────────────────

def _resolve_alias(name: str) -> str:
    """Map legacy profile names to current tier names."""
    return LEGACY_ALIASES.get(name, name)


def _tier_field(yaml_path: Path, tier: str, field: str) -> str:
    """Read a single field from a tier block under `tiers:` in models.yaml.
    Also consults the top-level `aliases:` table if `tier` is a legacy name."""
    tier = _resolve_alias(tier)
    in_tiers_section = False
    in_block = False
    with open(yaml_path) as f:
        for line in f:
            stripped = line.rstrip()
            if stripped == "tiers:":
                in_tiers_section = True
                continue
            if in_tiers_section and stripped and not stripped.startswith(" "):
                # Left the tiers: section (e.g. hit `aliases:`).
                in_tiers_section = False
                in_block = False
                continue
            if not in_tiers_section:
                continue
            # Tier key is at 2-space indent.
            if line.startswith(f"  {tier}:"):
                in_block = True
                continue
            if in_block:
                # Next sibling tier at 2-space indent ends the block.
                if line.startswith("  ") and not line.startswith("   "):
                    break
                m = re.match(rf"\s+{field}:\s+[\"']?(.+?)[\"']?\s*$", line)
                if m:
                    return m.group(1).strip()
    return ""


def get_default_profile(yaml_path: Path) -> str:
    with open(yaml_path) as f:
        for line in f:
            m = re.match(r"^default:\s+(\w+)", line)
            if m:
                return m.group(1)
    return "versatile"


def load_model_id(profile: str, yaml_path: Path = MODELS_YAML) -> str:
    """Return the Ollama/llama.cpp model_tag for a tier (or alias)."""
    # New schema uses `model_tag`; legacy used `id`. Try both for forward
    # compatibility in case the schema evolves further.
    for field in ("model_tag", "id"):
        val = _tier_field(yaml_path, profile, field)
        if val:
            return val
    sys.exit(f"Tier '{profile}' not found in {yaml_path}")


def load_all_model_ids(yaml_path: Path = MODELS_YAML) -> dict:
    """Return {tier: model_tag} for all tiers code_assist routes to."""
    return {p: load_model_id(p, yaml_path) for p in VALID_PROFILES}


# ── Prompt helpers ────────────────────────────────────────────────────────────

def load_system_prompt(mode: str) -> str:
    path = PROMPTS_DIR / f"{mode}.txt"
    if not path.exists():
        sys.exit(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8").strip()


def detect_mode(task_text: str) -> str:
    """Auto-detect the best conversation mode for a subtask based on keywords."""
    t = task_text.lower()
    if any(w in t for w in ["test", "unit", "assert", "pytest"]):
        return "test"
    if any(w in t for w in ["review", "check", "audit", "quality"]):
        return "review"
    if any(w in t for w in ["plan", "design", "steps", "roadmap"]):
        return "plan"
    if any(w in t for w in ["fix", "bug", "error", "crash", "broken"]):
        return "fix"
    return "explain"


def detect_specialization(task_text: str) -> str:
    """
    Detect the domain of a task to route it to the most capable model.
    Returns one of: 'vision', 'math', 'coding', 'general'.
    This is the per-agent specialization used by /multi and auto-routing.
    """
    t = task_text.lower()
    if any(w in t for w in VISION_KEYWORDS):
        return "vision"
    if any(w in t for w in MATH_KEYWORDS):
        return "math"
    if any(w in t for w in CODE_KEYWORDS):
        return "coding"
    return "general"


def find_vision_model(client: OpenAI) -> str | None:
    """
    Query LM Studio for a loaded vision-capable model.
    Returns the model ID if found, None if no vision model is loaded.
    """
    try:
        resp = client.models.list()
        for model in resp.data:
            mid = model.id.lower()
            if any(hint in mid for hint in VISION_MODEL_HINTS):
                return model.id
    except Exception:
        pass
    return None


def resolve_model_for_task(task_text: str, model_ids: dict, client: OpenAI) -> tuple[str, str]:
    """
    Choose the best model ID for a task based on its specialization.
    Returns (model_id, label) where label describes why this model was chosen.
    """
    spec = detect_specialization(task_text)

    if spec == "vision":
        vision_id = find_vision_model(client)
        if vision_id:
            return vision_id, f"vision model ({vision_id})"
        # Fallback: the dedicated vision tier if loaded locally, else highest_quality.
        return (
            model_ids.get("vision") or model_ids["highest_quality"],
            "vision tier (no LM Studio vision model detected; using Qwen3.6 mmproj if available)",
        )

    if spec == "math":
        return model_ids["highest_quality"], "highest_quality tier (math/reasoning)"

    if spec == "coding":
        return model_ids["coding"], "coding tier (code-specialized)"

    return model_ids["versatile"], "versatile tier (general)"


# ── Difficulty classifier (Claude Code sideQuery pattern) ─────────────────────

def classify_difficulty(user_message: str, client: OpenAI, fast_model_id: str) -> str:
    """
    Quick side-query using the fast model to classify task difficulty.
    Returns one of: EASY, MEDIUM, HARD, EXPERT.
    This mirrors the sideQuery.ts pattern from the Claude Code source.
    """
    try:
        resp = client.chat.completions.create(
            model=fast_model_id,
            messages=[{
                "role": "user",
                "content": (
                    "Classify this task with exactly ONE word from: EASY, MEDIUM, HARD, EXPERT.\n"
                    "EASY = simple question or short answer.\n"
                    "MEDIUM = needs reasoning or multi-step thinking.\n"
                    "HARD = complex code generation or algorithms.\n"
                    "EXPERT = architecture, research, or systems design.\n\n"
                    f"Task: {user_message}"
                )
            }],
            max_tokens=10,
            temperature=0
        )
        answer = resp.choices[0].message.content.strip().upper()
        for level in ["EASY", "MEDIUM", "HARD", "EXPERT"]:
            if level in answer:
                return level
    except Exception:
        pass
    return "MEDIUM"


# ── Jupyter integration ───────────────────────────────────────────────────────

def run_in_jupyter(code: str) -> str:
    """
    Execute Python code in the Jupyter container via its REST + WebSocket API.
    Returns the text output (stdout, stderr, or error traceback).
    Requires: pip install websocket-client requests
    """
    try:
        import requests
        import websocket
    except ImportError:
        return (
            "[Jupyter integration unavailable — run: pip install websocket-client requests]\n"
            "Copy the code above and paste it at: http://localhost:8888/lab"
        )

    headers = {"Authorization": f"Token {JUPYTER_TOKEN}"}
    base = JUPYTER_URL

    try:
        r = requests.post(f"{base}/api/kernels", headers=headers,
                          json={"name": "python3"}, timeout=10)
        r.raise_for_status()
        kid = r.json()["id"]
    except Exception as e:
        return f"[Could not connect to Jupyter at {base} — is the stack running? Error: {e}]"

    ws_url = f"ws://localhost:8888/api/kernels/{kid}/channels?token={JUPYTER_TOKEN}"
    output = []
    try:
        ws = websocket.create_connection(ws_url, timeout=30)
        msg_id = str(uuid.uuid4())
        ws.send(json.dumps({
            "header": {
                "msg_id": msg_id, "msg_type": "execute_request",
                "username": "", "session": str(uuid.uuid4()), "version": "5.3"
            },
            "parent_header": {},
            "metadata": {},
            "content": {"code": code, "silent": False, "store_history": True,
                        "user_expressions": {}, "allow_stdin": False},
            "buffers": [], "channel": "shell"
        }))

        while True:
            raw = ws.recv()
            msg = json.loads(raw)
            mt = msg.get("msg_type", "")
            content = msg.get("content", {})

            if mt == "stream":
                output.append(content.get("text", ""))
            elif mt in ("execute_result", "display_data"):
                data = content.get("data", {})
                output.append(data.get("text/plain", ""))
            elif mt == "error":
                output.append("\n".join(content.get("traceback", [])))
            elif mt == "status" and content.get("execution_state") == "idle":
                # Check parent matches our request
                if msg.get("parent_header", {}).get("msg_id") == msg_id:
                    break

        ws.close()
    except Exception as e:
        return f"[Jupyter execution error: {e}]"
    finally:
        try:
            import requests as _r
            _r.delete(f"{base}/api/kernels/{kid}", headers=headers, timeout=5)
        except Exception:
            pass

    result = "".join(output).strip()
    return result if result else "(no output)"


# ── Multi-agent helpers ───────────────────────────────────────────────────────

def run_subagent(task: str, mode: str, client: OpenAI, model_id: str, model_label: str = "") -> str:
    """Fresh single-turn conversation for one subtask. No shared history."""
    system = load_system_prompt(mode)
    resp = client.chat.completions.create(
        model=model_id,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": task}
        ],
        temperature=0.3
    )
    return resp.choices[0].message.content


def run_multi(task_desc: str, client: OpenAI, model_ids: dict, current_model_id: str) -> str:
    """
    Orchestrator → parallel workers → synthesizer.
    Mirrors the swarm/InProcessBackend pattern from Claude Code source.
    """
    print(f"\n{CYAN}[Orchestrator] Breaking task into subtasks...{RESET}")
    orch_resp = client.chat.completions.create(
        model=current_model_id,
        messages=[{
            "role": "user",
            "content": (
                "Break the following task into 2-4 discrete subtasks that can each be done "
                "independently. Return ONLY a numbered list, one subtask per line. No explanations.\n\n"
                f"Task: {task_desc}"
            )
        }],
        temperature=0
    )
    subtask_text = orch_resp.choices[0].message.content.strip()
    subtasks = [
        re.sub(r"^\d+[\.\)]\s*", "", line).strip()
        for line in subtask_text.splitlines()
        if line.strip() and re.match(r"^\d", line.strip())
    ]

    if not subtasks:
        return "[Could not parse subtasks. Try rephrasing your /multi command.]"

    print(f"\n{YELLOW}Subtasks identified:{RESET}")
    for i, s in enumerate(subtasks, 1):
        print(f"  {i}. {s}")

    answer = input(f"\n{CYAN}Run all {len(subtasks)} subtasks in parallel? [Y/n]: {RESET}").strip().lower()
    if answer == "n":
        return "[Cancelled.]"

    # Resolve the best specialized model for each subtask
    task_routing = {}
    print(f"\n{DIM}Routing subtasks to specialized models:{RESET}")
    for task in subtasks:
        mid, label = resolve_model_for_task(task, model_ids, client)
        task_routing[task] = (mid, label)
        print(f"  → {task[:55]}... [{label}]")

    # Parallel execution — LM Studio supports parallel: 4 for fast/quality models
    results = {}
    max_workers = min(len(subtasks), 4)
    print(f"\n{CYAN}[Launching {len(subtasks)} agents in parallel (max {max_workers} concurrent)]...{RESET}")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                run_subagent,
                task,
                detect_mode(task),
                client,
                task_routing[task][0],   # per-task specialized model ID
                task_routing[task][1]
            ): task
            for task in subtasks
        }
        for future in as_completed(futures):
            task = futures[future]
            try:
                results[task] = future.result()
                print(f"  {GREEN}✓ Agent done:{RESET} {task[:60]}...")
            except Exception as e:
                results[task] = f"[Error: {e}]"
                print(f"  {RED}✗ Agent failed:{RESET} {task[:60]}")

    # Synthesize
    print(f"\n{CYAN}[Synthesizing results...]{RESET}")
    synthesis_input = "\n\n".join(
        f"=== Subtask: {t} ===\n{r}" for t, r in results.items()
    )
    synth_resp = client.chat.completions.create(
        model=current_model_id,
        messages=[{
            "role": "user",
            "content": (
                "The following are results from parallel AI agents, each working on a subtask. "
                "Synthesize them into one clear, coherent response. Resolve any conflicts or overlaps.\n\n"
                + synthesis_input
            )
        }],
        temperature=0.3
    )
    return synth_resp.choices[0].message.content


# ── Main interactive loop ─────────────────────────────────────────────────────

def extract_code_blocks(text: str) -> list:
    """Find all fenced code blocks in a response."""
    return re.findall(r"```(?:\w+)?\n(.*?)```", text, re.DOTALL)


def stream_response(client: OpenAI, history: list, model_id: str) -> str:
    """Stream the assistant's response token by token. Returns full text."""
    full = []
    print(f"\n{GREEN}Assistant:{RESET} ", end="", flush=True)
    stream = client.chat.completions.create(
        model=model_id,
        messages=history,
        temperature=0.3,
        stream=True
    )
    for chunk in stream:
        delta = chunk.choices[0].delta.content or ""
        print(delta, end="", flush=True)
        full.append(delta)
    print()  # newline after response
    return "".join(full)


def compact_history(history: list, client: OpenAI, model_id: str) -> list:
    """
    Summarize conversation history into one message to free context window space.
    Mirrors the /compact command from Claude Code's compact.ts.
    """
    turns = [m for m in history if m["role"] != "system"]
    if not turns:
        print(f"{YELLOW}[Nothing to compact yet.]{RESET}")
        return history

    n = len(turns)
    convo_text = "\n".join(f"{m['role'].upper()}: {m['content'][:500]}" for m in turns)
    print(f"{CYAN}[Compacting {n} turns...]{RESET}")

    resp = client.chat.completions.create(
        model=model_id,
        messages=[{
            "role": "user",
            "content": (
                "Summarize this conversation concisely. Keep: all code written, all decisions made, "
                "key context and conclusions. Discard: pleasantries, repeated explanations, filler.\n\n"
                + convo_text
            )
        }],
        temperature=0
    )
    summary = resp.choices[0].message.content.strip()

    system_msg = history[0] if history and history[0]["role"] == "system" else None
    new_history = []
    if system_msg:
        new_history.append(system_msg)
    new_history.append({"role": "user", "content": f"[Previous conversation summary]\n{summary}"})

    print(f"{GREEN}[Compacted: {n} turns → 1 summary]{RESET}")
    return new_history


def print_banner(mode: str, profile: str, model_id: str, auto_route: bool):
    print(f"\n{'='*60}")
    print(f"  Kit's AI Coding Assistant")
    print(f"  Mode   : {mode}")
    print(f"  Profile: {profile}  ({model_id})")
    if auto_route:
        print(f"  Routing: auto (task difficulty selects best model)")
    print(f"{'='*60}")
    print(f"  Commands: /clear  /compact  /mode <name>  /file <path>")
    print(f"            /multi <task>  /history  /exit")
    print(f"  Ctrl+C to quit")
    print(f"{'='*60}\n")


def main():
    args = _parser.parse_args()

    # Resolve starting profile
    if not args.profile or args.profile == "fixed":
        profile = get_default_profile(MODELS_YAML)
        auto_route = args.profile != "fixed"
    else:
        profile = args.profile
        auto_route = False

    model_ids = load_all_model_ids()
    model_id  = model_ids[profile]

    client = OpenAI(base_url=LMSTUDIO_URL, api_key="lmstudio")

    mode    = args.mode
    history = [{"role": "system", "content": load_system_prompt(mode)}]

    print_banner(mode, profile, model_id, auto_route)

    while True:
        try:
            user_input = input(f"{CYAN}You> {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{DIM}Goodbye.{RESET}")
            break

        if not user_input:
            continue

        # ── Built-in slash commands ────────────────────────────────────────────
        if user_input in ("/exit", "/quit"):
            print(f"{DIM}Goodbye.{RESET}")
            break

        if user_input == "/clear":
            history = [{"role": "system", "content": load_system_prompt(mode)}]
            print(f"{YELLOW}[History cleared.]{RESET}")
            continue

        if user_input == "/compact":
            history = compact_history(history, client, model_id)
            continue

        if user_input == "/history":
            turns = len([m for m in history if m["role"] != "system"])
            print(f"{YELLOW}[{turns} turns in history]{RESET}")
            continue

        if user_input.startswith("/mode "):
            new_mode = user_input.split(None, 1)[1].strip()
            if new_mode not in VALID_MODES:
                print(f"{RED}Unknown mode '{new_mode}'. Choose from: {', '.join(VALID_MODES)}{RESET}")
                continue
            mode    = new_mode
            history = [{"role": "system", "content": load_system_prompt(mode)}]
            print(f"{GREEN}[Switched to {mode} mode. History cleared.]{RESET}")
            continue

        if user_input.startswith("/file "):
            path_str = user_input.split(None, 1)[1].strip()
            try:
                content = Path(path_str).read_text(encoding="utf-8")
                injected = f"[File: {path_str}]\n```\n{content}\n```"
                history.append({"role": "user", "content": injected})
                print(f"{GREEN}[File loaded: {path_str} ({len(content)} chars)]{RESET}")
                print(f"{DIM}File contents added to conversation. Ask your question.{RESET}")
            except Exception as e:
                print(f"{RED}[Could not read file: {e}]{RESET}")
            continue

        if user_input.startswith("/multi "):
            task_desc = user_input.split(None, 1)[1].strip()
            result = run_multi(task_desc, client, model_ids, model_id)
            print(f"\n{GREEN}[Multi-agent synthesis]{RESET}\n{result}")
            history.append({"role": "assistant", "content": result})
            continue

        # ── Auto-route to best model based on specialization + difficulty ────────
        active_model_id = model_id
        if auto_route:
            # 1. Check domain specialization first (vision, math, coding, general)
            spec = detect_specialization(user_input)
            if spec == "vision":
                vision_id = find_vision_model(client)
                if vision_id:
                    active_model_id = vision_id
                    print(f"{DIM}[Specialization: vision → {vision_id}]{RESET}")
                else:
                    active_model_id = model_ids.get("vision") or model_ids["highest_quality"]
                    print(f"{YELLOW}[Vision task detected — using vision tier (or highest_quality fallback). "
                          f"Load Qwen3.6 + mmproj via scripts/setup-models.sh for true vision.]{RESET}")
            elif spec in ("math", "coding"):
                # Coding tier for code, highest_quality for pure math
                routed_profile = "coding" if spec == "coding" else "highest_quality"
                active_model_id = model_ids[routed_profile]
                print(f"{DIM}[Specialization: {spec} → {routed_profile} tier]{RESET}")
            else:
                # 2. Fall back to difficulty-based routing for general tasks
                difficulty = classify_difficulty(user_input, client, model_ids["fast"])
                routed_profile = DIFFICULTY_MAP.get(difficulty, profile)
                active_model_id = model_ids[routed_profile]
                if routed_profile != profile:
                    print(f"{DIM}[Task: {difficulty} → using {routed_profile} model]{RESET}")

        # Auto-compact warning
        turn_count = len([m for m in history if m["role"] == "user"])
        if turn_count > 0 and turn_count % 30 == 0:
            print(f"{YELLOW}[Tip: conversation is getting long ({turn_count} turns). "
                  f"Type /compact to summarize and free up context.]{RESET}")

        # ── Normal message → LM Studio ────────────────────────────────────────
        history.append({"role": "user", "content": user_input})
        response = stream_response(client, history, active_model_id)
        history.append({"role": "assistant", "content": response})

        # ── Offer to run code blocks in Jupyter ───────────────────────────────
        code_blocks = extract_code_blocks(response)
        if code_blocks:
            for i, code in enumerate(code_blocks):
                label = f" #{i+1}" if len(code_blocks) > 1 else ""
                try:
                    run_it = input(f"{YELLOW}Run code block{label} in Jupyter? [y/N]: {RESET}").strip().lower()
                except (KeyboardInterrupt, EOFError):
                    break
                if run_it == "y":
                    print(f"{CYAN}[Running in Jupyter...]{RESET}")
                    output = run_in_jupyter(code)
                    print(f"\n{GREEN}Jupyter output:{RESET}\n{output}")
                    # Feed output back so AI can comment on it
                    history.append({
                        "role": "user",
                        "content": f"I ran the code in Jupyter. Output:\n```\n{output}\n```"
                    })


if __name__ == "__main__":
    main()
