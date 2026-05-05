"""Probe one MMLU question against a tier with think=on and dump
the raw streaming response, including whether reasoning_content is
being populated separately from content."""
import json
import sys
from pathlib import Path
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

API = "http://127.0.0.1:18000"
PROMPT = (
    "Subject: high_school_mathematics\n\n"
    "Question: Find all c in Z_3 such that Z_3[x]/(x^2 + c) is a field.\n\n"
    "Choices:\n"
    "  A. 0\n"
    "  B. 1\n"
    "  C. 2\n"
    "  D. 3\n\n"
    "Respond with the single letter (A, B, C, or D) of the "
    "correct choice on the last line, prefixed by '####'."
)


def probe(tier: str, think: bool):
    body = {
        "model": f"tier.{tier}",
        "stream": True,
        "think": think,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": PROMPT}],
    }
    req = urllib.request.Request(
        API + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "accept": "text/event-stream"},
    )
    content = []
    reasoning = []
    other_keys = set()
    finish_reason = None
    with urllib.request.urlopen(req, timeout=120) as r:
        for raw in r:
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            for ch in obj.get("choices", []):
                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]
                delta = ch.get("delta") or {}
                for k in delta.keys():
                    if k not in ("content", "reasoning_content", "role"):
                        other_keys.add(k)
                if delta.get("content"):
                    content.append(delta["content"])
                if delta.get("reasoning_content"):
                    reasoning.append(delta["reasoning_content"])
    print(f"--- tier={tier} think={think} ---")
    print(f"finish_reason: {finish_reason}")
    print(f"reasoning_content len: {sum(len(r) for r in reasoning)} chars")
    print(f"content len:           {sum(len(c) for c in content)} chars")
    print(f"other delta keys: {other_keys}")
    print("--- reasoning_content (first 600 chars) ---")
    print("".join(reasoning)[:600])
    print("--- content (first 600 chars) ---")
    print("".join(content)[:600])
    print("--- reasoning (last 600 chars) ---")
    print("".join(reasoning)[-600:])
    print("--- content (last 200 chars) ---")
    print("".join(content)[-200:])
    # Run the grader to see if answer is extracted
    from backend.eval.graders import score
    from backend.eval.datasets import Problem
    p = Problem(kind="mmlu", id="probe", prompt=PROMPT, answer="B", meta={})
    full = "".join(reasoning) + "".join(content)
    print(f"--- grader on full text: {'PASS' if score(p, full) else 'fail'}")
    print(f"--- grader on content-only: {'PASS' if score(p, ''.join(content)) else 'fail'}")
    print()


if __name__ == "__main__":
    tier = sys.argv[1] if len(sys.argv) > 1 else "fast"
    think = sys.argv[2] != "off" if len(sys.argv) > 2 else True
    probe(tier, think)
