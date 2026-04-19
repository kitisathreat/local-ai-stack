"""Unit tests for backend/tools/registry.py.

Build the registry against the real tools/ directory so we also get a
smoke-test that every legacy tool still imports cleanly under Phase 5
auto-discovery.
"""

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def registry():
    from backend.tools.registry import build_registry
    return build_registry(tools_dir=ROOT / "tools", config_dir=ROOT / "config")


# ── Coverage ────────────────────────────────────────────────────────────

def test_registry_discovers_tools(registry):
    # Expect at least the simple, dep-free tools to be discovered.
    required = [
        "calculator.calculate",
        "datetime_tool",
    ]
    # Collapse module-only names and check at least one method of each module.
    names = list(registry.tools.keys())
    assert len(names) > 10, f"Expected many tools, got {len(names)}"
    assert any(n.startswith("calculator.") for n in names), (
        f"calculator.* missing. Sample: {names[:20]}"
    )
    assert any(n.startswith("datetime_tool.") for n in names), (
        f"datetime_tool.* missing."
    )


# ── Schema shape ────────────────────────────────────────────────────────

def test_tool_schemas_are_openai_shaped(registry):
    sample_name = next(iter(registry.tools))
    schema = registry.tools[sample_name].schema
    assert schema["type"] == "function"
    fn = schema["function"]
    assert "name" in fn and "description" in fn
    params = fn["parameters"]
    assert params["type"] == "object"
    assert "properties" in params and "required" in params


def test_schemas_exclude_injected_params(registry):
    for name, t in registry.tools.items():
        props = t.schema["function"]["parameters"]["properties"]
        assert "__user__" not in props, f"{name} schema leaked __user__"
        assert "__event_emitter__" not in props, f"{name} leaked __event_emitter__"
        assert "self" not in props


# ── Dispatch ────────────────────────────────────────────────────────────

def test_dispatch_calculator(registry):
    from backend.tools.executor import dispatch_tool_call
    call = {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "calculator.calculate",
            "arguments": '{"expression": "2 + 2"}',
        },
    }
    result = asyncio.run(dispatch_tool_call(call, registry))
    assert result["role"] == "tool"
    assert result["tool_call_id"] == "call_1"
    assert "4" in result["content"]


def test_dispatch_unknown_tool(registry):
    from backend.tools.executor import dispatch_tool_call
    call = {
        "id": "call_bad",
        "type": "function",
        "function": {"name": "nonexistent.foo", "arguments": "{}"},
    }
    result = asyncio.run(dispatch_tool_call(call, registry))
    assert "Unknown tool" in result["content"]


def test_dispatch_bad_json(registry):
    from backend.tools.executor import dispatch_tool_call
    call = {
        "id": "call_bad_json",
        "type": "function",
        "function": {
            "name": "calculator.calculate",
            "arguments": "{not-json",
        },
    }
    result = asyncio.run(dispatch_tool_call(call, registry))
    assert "Invalid JSON" in result["content"]


def test_dispatch_many(registry):
    from backend.tools.executor import dispatch_many
    calls = [
        {"id": "a", "function": {"name": "calculator.calculate", "arguments": '{"expression": "1+1"}'}},
        {"id": "b", "function": {"name": "calculator.calculate", "arguments": '{"expression": "3*3"}'}},
    ]
    results = asyncio.run(dispatch_many(calls, registry))
    assert len(results) == 2
    assert "2" in results[0]["content"]
    assert "9" in results[1]["content"]
