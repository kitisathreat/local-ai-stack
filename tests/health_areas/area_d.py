"""Area D — Model loading probes."""

from __future__ import annotations
import asyncio
import json
import os
import pathlib
import time


def _http_get(url: str, timeout: float = 10.0):
    try:
        import httpx
        r = httpx.get(url, timeout=timeout)
        return r.status_code, r.text
    except Exception as e:
        return None, str(e)


def _http_post(url: str, payload: dict, timeout: float = 90.0):
    try:
        import httpx
        r = httpx.post(url, json=payload, timeout=timeout)
        return r.status_code, r.text
    except Exception as e:
        return None, str(e)


def run() -> list[dict]:
    results = []

    def probe(name: str, fn) -> None:
        try:
            status, detail, fix_hint = fn()
        except Exception as e:
            status, detail, fix_hint = "FAIL", str(e), "Run LocalAIStack.ps1 -Start"
        results.append({"area": "D", "test": name, "status": status,
                        "detail": detail, "fix_hint": fix_hint})

    # Ollama running
    def _ollama():
        code, body = _http_get("http://127.0.0.1:11434/api/version")
        if code == 200:
            try:
                ver = json.loads(body).get("version", "?")
            except Exception:
                ver = body[:60]
            return "PASS", f"Ollama {ver}", ""
        return "FAIL", body[:200], "Run LocalAIStack.ps1 -Start"

    probe("ollama_running", _ollama)

    # Fast tier responds (non-streaming, short timeout)
    def _fast_tier():
        payload = {
            "model": "fast",
            "messages": [{"role": "user", "content": "Reply with the word OK only."}],
            "stream": False,
            "max_tokens": 5,
        }
        code, body = _http_post("http://127.0.0.1:18000/v1/chat/completions",
                                payload, timeout=90)
        if code == 200:
            return "PASS", "fast tier responded", ""
        if code == 401:
            return "WARN", "Auth required — fast tier reachable but not tested", \
                   "Log in via GUI and retry, or use ADMIN_EMAILS bypass"
        if code is None:
            return "FAIL", body, "Run LocalAIStack.ps1 -Start"
        return "WARN", f"HTTP {code}: {body[:150]}", "Pull fast tier model via admin panel"

    probe("fast_tier_responds", _fast_tier)

    # Qdrant running
    def _qdrant():
        code, body = _http_get("http://127.0.0.1:6333/healthz")
        if code == 200:
            return "PASS", "Qdrant healthy", ""
        if code is None:
            return "FAIL", body, "Run LocalAIStack.ps1 -Start"
        return "WARN", f"HTTP {code}: {body[:100]}", "Restart Qdrant: LocalAIStack.ps1 -Start"

    probe("qdrant_running", _qdrant)

    # Vision GGUF present
    def _vision_gguf():
        # Check both dev and installed layout
        candidates = []
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            candidates.append(pathlib.Path(local_appdata) / "LocalAIStack" / "data" / "models" / "vision.gguf")
        repo = pathlib.Path(__file__).resolve().parents[3]
        candidates.append(repo / "data" / "models" / "vision.gguf")

        for p in candidates:
            if p.exists():
                size_gb = p.stat().st_size / 1e9
                return "PASS", f"{p} ({size_gb:.1f} GB)", ""
        return "WARN", "vision.gguf not found", \
               "Download via admin panel or LocalAIStack.ps1 -CheckUpdates"

    probe("vision_gguf_present", _vision_gguf)

    # llama-server running
    def _llama():
        code, body = _http_get("http://127.0.0.1:8001/health")
        if code == 200:
            return "PASS", "llama-server healthy", ""
        if code is None:
            return "WARN", body, \
                   "Vision tier optional — only needed for image inputs. Run -Start to enable."
        return "WARN", f"HTTP {code}: {body[:100]}", "Check logs\\llama-server.log"

    probe("llama_server_running", _llama)

    # Embedding model available (RAG/memory requires nomic-embed-text)
    def _embedding():
        payload = {
            "model": "nomic-embed-text",
            "input": "test",
        }
        code, body = _http_post("http://127.0.0.1:11434/api/embeddings", payload, timeout=30)
        if code == 200:
            return "PASS", "nomic-embed-text available", ""
        if code is None:
            return "FAIL", body, "Run LocalAIStack.ps1 -Start then pull nomic-embed-text"
        # 404 means model not pulled
        return "WARN", f"HTTP {code} — embedding model not pulled", \
               "Run: ollama pull nomic-embed-text"

    probe("embedding_model", _embedding)

    return results
