"""End-to-end test for the multi-agent tool-call loop (#14).

Confirms that a worker wired through Orchestrator actually:
  1. Receives tool schemas on the initial chat_stream call.
  2. Dispatches any tool_calls the model returns through the registry.
  3. Feeds the tool-role results back into a second chat_stream call.
  4. Returns the model's final text once no more tool_calls are emitted.

Tests the isolated `_worker_with_tools` helper directly — it's where the
actual loop lives — rather than driving the whole orchestrator run, which
would also involve a planner + synthesis pass we don't need for this test.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


class FakeOllamaClient:
    """Records calls to chat_stream and replays a scripted sequence."""

    def __init__(self, *, scripted_chunks: list[list[dict]]):
        self.calls: list[dict] = []
        self._scripts = list(scripted_chunks)

    def chat_stream(self, tier, messages, *, think=False, tools=None, **kw):
        self.calls.append({"messages": messages, "tools": tools, "think": think})
        chunks = self._scripts.pop(0) if self._scripts else [{"done": True, "message": {}}]

        async def _gen():
            for c in chunks:
                yield c

        return _gen()


class FakeRegistry:
    """Minimal stand-in for ToolRegistry — only dispatch_many uses it."""

    def __init__(self):
        self.tools = {
            "calculator.calculate": SimpleNamespace(
                name="calculator.calculate",
                schema={
                    "type": "function",
                    "function": {
                        "name": "calculator.calculate",
                        "description": "Evaluate a math expression.",
                        "parameters": {
                            "type": "object",
                            "properties": {"expression": {"type": "string"}},
                            "required": ["expression"],
                        },
                    },
                },
                handler=lambda expression: 391,
                default_enabled=True,
                requires_service=None,
            ),
        }

    def get(self, name):
        return self.tools.get(name)

    def is_airgap_safe(self, name):
        return True

    def all_schemas(self, only_enabled=True, *, airgap=False):
        return [t.schema for t in self.tools.values()]


def test_worker_with_tools_dispatches_and_loops():
    from backend.orchestrator import Orchestrator

    fake_client = FakeOllamaClient(
        scripted_chunks=[
            # Round 1: model emits a tool_call.
            [
                {"message": {"content": "", "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {
                        "name": "calculator.calculate",
                        "arguments": '{"expression": "17*23"}',
                    },
                }]}},
                {"done": True, "message": {}},
            ],
            # Round 2: model answers using the tool result.
            [
                {"message": {"content": "The answer is 391."}},
                {"done": True, "message": {}},
            ],
        ],
    )
    registry = FakeRegistry()

    # Orchestrator needs cfg/scheduler/backends to construct, but
    # _worker_with_tools only touches self.tools — minimal stubs are fine.
    orch = Orchestrator(
        config=SimpleNamespace(),
        scheduler=SimpleNamespace(),
        backends={"ollama": fake_client},
        tools=registry,
    )

    tier = SimpleNamespace(backend="ollama")
    output = asyncio.run(
        orch._worker_with_tools(
            worker_client=fake_client,
            worker_tier=tier,
            messages=[{"role": "user", "content": "what is 17 * 23?"}],
            think_workers=False,
            worker_tool_schemas=registry.all_schemas(),
        )
    )

    # Two round-trips to the model — one that emitted a tool_call, one final.
    assert len(fake_client.calls) == 2, f"Expected 2 calls, got {len(fake_client.calls)}"

    # Schemas flowed into the initial call.
    assert fake_client.calls[0]["tools"] is not None
    assert any(
        (s.get("function") or {}).get("name") == "calculator.calculate"
        for s in fake_client.calls[0]["tools"]
    )

    # Second call's message list now has the tool-role result appended.
    second_msgs = fake_client.calls[1]["messages"]
    assert any(isinstance(m, dict) and m.get("role") == "tool" for m in second_msgs)
    assert any(isinstance(m, dict) and m.get("tool_calls") for m in second_msgs)

    assert "391" in output


def test_worker_with_tools_respects_max_turns():
    """If the model keeps asking for tools forever, the loop must exit."""
    from backend.orchestrator import Orchestrator

    infinite_tool_call = [
        {"message": {"content": "", "tool_calls": [{
            "id": "call_x", "type": "function",
            "function": {
                "name": "calculator.calculate",
                "arguments": '{"expression": "1"}',
            },
        }]}},
        {"done": True, "message": {}},
    ]
    fake_client = FakeOllamaClient(scripted_chunks=[infinite_tool_call] * 20)

    orch = Orchestrator(
        config=SimpleNamespace(),
        scheduler=SimpleNamespace(),
        backends={"ollama": fake_client},
        tools=FakeRegistry(),
    )
    tier = SimpleNamespace(backend="ollama")
    asyncio.run(
        orch._worker_with_tools(
            worker_client=fake_client,
            worker_tier=tier,
            messages=[{"role": "user", "content": "loop forever"}],
            think_workers=False,
            worker_tool_schemas=FakeRegistry().all_schemas(),
        )
    )
    # Cap is max_turns+1 (6). Anything higher = runaway loop.
    assert len(fake_client.calls) <= 6


def test_orchestrator_stores_tool_registry():
    """Regression guard on #14: fails if the registry isn't threaded into
    Orchestrator(..., tools=...)."""
    from backend.orchestrator import Orchestrator

    reg = FakeRegistry()
    orch = Orchestrator(
        config=SimpleNamespace(),
        scheduler=SimpleNamespace(),
        backends={},
        tools=reg,
    )
    assert orch.tools is reg
