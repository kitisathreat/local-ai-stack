"""Dispatch tool_calls emitted by the model.

In the Ollama chat API, a tool_call looks like:
    {
      "id": "call_xyz",
      "type": "function",
      "function": {
        "name": "calculator.calculate",
        "arguments": "{\"expression\": \"2+2\"}"
      }
    }

We parse the arguments, look up the handler in the registry, call it
(inject __user__ / __event_emitter__ if the function accepts them), and
return a message with role=tool + tool_call_id + stringified result to
feed back to the model for a follow-up turn.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any, Callable

from .. import airgap
from .registry import ToolEntry, ToolRegistry


logger = logging.getLogger(__name__)


async def dispatch_tool_call(
    call: dict,
    registry: ToolRegistry,
    user: dict | None = None,
    event_emitter: Callable[[dict], Any] | None = None,
) -> dict:
    """Run one tool call. Returns the OpenAI-style tool-role message to
    append to the conversation."""
    call_id = call.get("id") or f"call_{id(call)}"
    fn = (call.get("function") or {})
    name = fn.get("name", "")
    raw_args = fn.get("arguments") or "{}"

    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
    except json.JSONDecodeError as e:
        return _error_message(call_id, name, f"Invalid JSON arguments: {e}")

    entry = registry.get(name)
    if not entry:
        return _error_message(call_id, name, f"Unknown tool: {name}")

    if airgap.is_enabled() and not registry.is_airgap_safe(name):
        return _error_message(
            call_id, name,
            "Airgap mode is ON — this tool requires an external service "
            "and has been blocked. Disable airgap in the admin dashboard "
            "to use it.",
        )

    return await _invoke(entry, args, user, event_emitter, call_id)


async def dispatch_many(
    calls: list[dict],
    registry: ToolRegistry,
    user: dict | None = None,
    event_emitter: Callable[[dict], Any] | None = None,
) -> list[dict]:
    """Run multiple tool calls in parallel."""
    return await asyncio.gather(*(
        dispatch_tool_call(c, registry, user, event_emitter) for c in calls
    ))


async def _invoke(
    entry: ToolEntry,
    args: dict,
    user: dict | None,
    event_emitter: Callable | None,
    call_id: str,
) -> dict:
    handler = entry.handler
    # Prefer the cached injected-param set when the entry carries one
    # (the lazy-load path; #27) — falling back to live inspection for
    # eager entries and hand-constructed registries in tests.
    injected: set[str]
    entry_injected = getattr(entry, "injected_params", None)
    if entry_injected:
        injected = set(entry_injected)
    else:
        try:
            sig = inspect.signature(handler)
            injected = set(sig.parameters.keys())
        except (TypeError, ValueError):
            injected = set()
    # Decide which injected params the handler actually wants.
    kw = dict(args)
    if "__user__" in injected:
        kw["__user__"] = user
    if "__event_emitter__" in injected:
        kw["__event_emitter__"] = event_emitter
    if "__metadata__" in injected:
        kw["__metadata__"] = {}
    if "__request__" in injected:
        kw["__request__"] = None

    try:
        if inspect.iscoroutinefunction(handler):
            result = await handler(**kw)
        else:
            # Legacy tools are sync; run in a thread so blocking I/O
            # doesn't stall the event loop.
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, lambda: handler(**kw))
    except TypeError as e:
        # Most likely bad args. Return the error so the model can retry.
        logger.info("Tool %s TypeError: %s", entry.name, e)
        return _error_message(call_id, entry.name, f"Bad arguments: {e}")
    except Exception as e:
        logger.exception("Tool %s raised", entry.name)
        return _error_message(call_id, entry.name, f"Error: {e}")

    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": entry.name,
        "content": _stringify(result),
    }


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


def _error_message(call_id: str, name: str, msg: str) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name or "unknown",
        "content": msg,
    }
