"""Dump raw SSE deltas from /v1/chat/completions through the backend."""
import json
import sys
import urllib.request

API = "http://127.0.0.1:18000"

PROMPT = (
    "Subject: high_school_mathematics\n\n"
    "Question: Find all c in Z_3 such that Z_3[x]/(x^2 + c) is a field.\n\n"
    "Choices:\n  A. 0\n  B. 1\n  C. 2\n  D. 3\n\n"
    "Respond with the single letter (A, B, C, or D) of the "
    "correct choice on the last line, prefixed by '####'."
)


def main():
    tier = sys.argv[1] if len(sys.argv) > 1 else "fast"
    think = sys.argv[2] != "off" if len(sys.argv) > 2 else True
    body = {
        "model": f"tier.{tier}",
        "stream": True,
        "think": think,
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": PROMPT}],
    }
    req = urllib.request.Request(
        API + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "accept": "text/event-stream"},
    )
    n_chunks = 0
    finish = None
    delta_keys = {}
    rc_total = 0
    c_total = 0
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
            n_chunks += 1
            for ch in obj.get("choices", []):
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
                delta = ch.get("delta") or {}
                for k in delta:
                    delta_keys[k] = delta_keys.get(k, 0) + 1
                if delta.get("content"):
                    c_total += len(delta["content"])
                if delta.get("reasoning_content"):
                    rc_total += len(delta["reasoning_content"])
    print(f"chunks: {n_chunks}")
    print(f"delta_keys hit counts: {delta_keys}")
    print(f"content total chars: {c_total}")
    print(f"reasoning_content total chars: {rc_total}")
    print(f"finish_reason: {finish}")


if __name__ == "__main__":
    main()
