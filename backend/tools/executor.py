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


def _validate_args(entry: ToolEntry, args: dict) -> str | None:
    """Lightweight validator against the tool's registered JSON schema
    (#68). Rejects unknown property types and missing required fields;
    returns a human-readable error string on violation, else None.

    We don't pull in `jsonschema` as a dep — only the primitive types
    we emit from `method_to_schema` are checked, which covers the
    registry's entire surface today."""
    if not isinstance(args, dict):
        return "arguments must be a JSON object"
    fn_schema = ((entry.schema or {}).get("function") or {}).get("parameters") or {}
    properties = fn_schema.get("properties") or {}
    required = fn_schema.get("required") or []
    for key in required:
        if key not in args:
            return f"missing required argument: {key}"
    _TYPE_CHECKS = {
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "array": lambda v: isinstance(v, list),
        "object": lambda v: isinstance(v, dict),
    }
    for key, val in args.items():
        spec = properties.get(key)
        if not spec:
            continue
        t = spec.get("type")
        if not t:
            continue
        check = _TYPE_CHECKS.get(t)
        if check is not None and not check(val):
            return f"argument {key!r} must be of type {t}"
    return None


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

    # #68: validate model-supplied args against the registered schema
    # before we hand them to the handler. Rejections come back to the
    # model as tool-role errors so it can self-correct.
    err = _validate_args(entry, args)
    if err:
        return _error_message(call_id, name, f"Invalid arguments: {err}")

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
    sig = inspect.signature(handler)
    # Decide which injected params the handler actually wants.
    kw = dict(args)
    if "__user__" in sig.parameters:
        kw["__user__"] = user
    if "__event_emitter__" in sig.parameters:
        kw["__event_emitter__"] = event_emitter
    if "__metadata__" in sig.parameters:
        kw["__metadata__"] = {}
    if "__request__" in sig.parameters:
        kw["__request__"] = None

    # #69: every tool invocation runs under a soft timeout so a slow
    # or adversarial handler can't tie up an event loop worker
    # indefinitely. Per-tool overrides come from tools.yaml; the
    # default is 30s.
    timeout_s = max(1.0, float(getattr(entry, "timeout_s", 30.0) or 30.0))
    try:
        if inspect.iscoroutinefunction(handler):
            coro = handler(**kw)
        else:
            # Legacy tools are sync; run in a thread so blocking I/O
            # doesn't stall the event loop.
            loop = asyncio.get_running_loop()
            coro = loop.run_in_executor(None, lambda: handler(**kw))
        result = await asyncio.wait_for(coro, timeout=timeout_s)
    except asyncio.TimeoutError:
        logger.warning("Tool %s timed out after %.1fs", entry.name, timeout_s)
        return _error_message(
            call_id, entry.name,
            f"Error: tool timed out after {timeout_s:.0f}s",
        )
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
