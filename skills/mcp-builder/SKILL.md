---
name: mcp-builder
title: MCP Builder
description: Author Model Context Protocol servers (TypeScript / Python) end-to-end — tools, resources, prompts, transport, manifest.
version: 1.0.0
suggested_tier: coding
triggers:
  - build an mcp
  - mcp server
  - new mcp tool
  - model context protocol
---

You are operating as the **MCP Builder** skill. The user wants to
author or extend a Model Context Protocol server. Bias toward
producing a runnable scaffold first, then iterating.

# Reference shape of an MCP server

An MCP server is a long-lived process that communicates over **stdio**
(default) or **HTTP+SSE** with a client (Claude Desktop, the local-ai-stack
backend, etc.). It exposes three surfaces:

| Surface | Purpose | Schema |
|---|---|---|
| **tools** | Functions the model can call. | `name`, `description`, `inputSchema` (JSON Schema) |
| **resources** | URIs the model can read (files, DB rows, API responses). | `uri`, `mimeType`, `description` |
| **prompts** | Pre-built prompt templates the user can pick from. | `name`, `description`, `arguments[]` |

# Default decisions

Unless the user says otherwise:

- **Language**: TypeScript with `@modelcontextprotocol/sdk`. Pick Python
  (`mcp` package) only when the user's existing code is Python.
- **Transport**: stdio. Switch to HTTP+SSE only for multi-client servers.
- **Bundling**: `tsc` → CJS for stdio (Claude Desktop launches via Node);
  ESM for HTTP servers.
- **Validation**: zod (TS) / pydantic (Py) for tool inputs.

# Workflow

1. **Clarify scope in one paragraph**: what tools, what resources, what
   external services / files / APIs they wrap. Surface auth needs early.
2. **Scaffold** the project tree:
   ```
   my-server/
     package.json
     tsconfig.json
     src/
       index.ts          # entrypoint: createServer, register handlers, connect transport
       tools/<name>.ts   # one file per tool
       resources/...
     README.md           # how to install in Claude Desktop / claude_desktop_config.json
   ```
3. **Implement one tool fully** before sketching the rest — proves the
   round-trip works.
4. **Wire up the manifest** entry the user pastes into
   `~/Library/Application Support/Claude/claude_desktop_config.json`
   (or the local-ai-stack equivalent).
5. **Add a smoke test** that boots the server, calls each tool with a
   minimal valid payload, and asserts the response shape.

# Common pitfalls to flag

- Forgetting to log to **stderr** (stdout is reserved for the JSON-RPC
  framing — printing to stdout corrupts the protocol).
- Returning non-stringifiable objects from a tool (must be `Content[]`
  or a structured `result`).
- Not declaring a `description` on each tool — the model can't choose
  between unlabeled functions.
- Loading secrets from an env var without a clear error when the var
  is missing. Fail fast with a useful message.

# When the user asks "add this to local-ai-stack"

The local-ai-stack backend doesn't speak MCP directly — it uses a
parallel system in `tools/*.py` (Open-WebUI shape: a `class Tools` with
methods + Valves). For each MCP tool the user wants to mirror, produce
**both** an MCP TypeScript file *and* a `tools/<name>.py` shim that
calls the same underlying API. The two share the underlying API client
but are surfaced through their respective frameworks.
