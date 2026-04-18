"""
title: n8n Workflow Trigger
author: local-ai-stack
description: Trigger n8n automation workflows from chat. Send data to webhooks, run scheduled workflows on-demand, and receive workflow results back in conversation.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import os
import httpx
import json
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


class Tools:
    class Valves(BaseModel):
        N8N_URL: str = Field(
            default="http://n8n:5678",
            description="Base URL of your n8n instance",
        )
        N8N_API_KEY: str = Field(
            default_factory=lambda: os.environ.get("N8N_API_KEY", ""),
            description="Optional n8n API key (create at n8n > Settings > API)",
        )
        TIMEOUT: int = Field(default=30, description="Webhook response timeout in seconds")

    def __init__(self):
        self.valves = self.Valves()

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.valves.N8N_API_KEY:
            h["X-N8N-API-KEY"] = self.valves.N8N_API_KEY
        return h

    async def trigger_webhook(
        self,
        webhook_path: str,
        payload: str = "{}",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Trigger an n8n webhook workflow and return its response.
        Set up a Webhook node in n8n with the path you specify here.
        :param webhook_path: The webhook path configured in n8n (e.g. "send-email", "process-data", "daily-report")
        :param payload: JSON data to send to the webhook (e.g. '{"message": "hello", "priority": "high"}')
        :return: Response from the n8n workflow
        """
        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": f"Triggering n8n webhook: {webhook_path}", "done": False}}
            )

        try:
            data = json.loads(payload) if payload.strip() else {}
        except json.JSONDecodeError:
            data = {"message": payload}

        # Add metadata
        data["_source"] = "local-ai-stack"
        if __user__:
            data["_user"] = __user__.get("name", "")

        url = f"{self.valves.N8N_URL}/webhook/{webhook_path.lstrip('/')}"

        try:
            async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as client:
                resp = await client.post(url, json=data, headers=self._headers())

            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": "Workflow completed", "done": True}}
                )

            if resp.status_code == 404:
                return (
                    f"Webhook not found: `{webhook_path}`\n\n"
                    f"To create this webhook in n8n:\n"
                    f"1. Open n8n at {self.valves.N8N_URL}\n"
                    f"2. Create a new workflow\n"
                    f"3. Add a **Webhook** trigger node\n"
                    f"4. Set the path to: `{webhook_path}`\n"
                    f"5. Activate the workflow"
                )

            try:
                result = resp.json()
                if isinstance(result, (dict, list)):
                    return f"## n8n Workflow Response\n```json\n{json.dumps(result, indent=2)}\n```"
                return f"## n8n Workflow Response\n{result}"
            except Exception:
                return f"## n8n Workflow Response\n{resp.text[:1000]}"

        except httpx.ConnectError:
            return (
                f"Cannot connect to n8n at {self.valves.N8N_URL}\n"
                f"Ensure n8n is running: `docker compose up -d n8n`\n"
                f"Then open: {self.valves.N8N_URL}"
            )
        except httpx.TimeoutException:
            return f"Webhook timed out after {self.valves.TIMEOUT}s. The workflow may still be running."
        except Exception as e:
            return f"n8n trigger error: {str(e)}"

    async def list_workflows(
        self,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List active workflows in your n8n instance (requires n8n API key).
        :return: List of workflow names, IDs, and active status
        """
        if not self.valves.N8N_API_KEY:
            return (
                "n8n API key required to list workflows.\n"
                f"1. Open n8n: {self.valves.N8N_URL}\n"
                "2. Go to Settings > API > Create API key\n"
                "3. Add it in Open WebUI > Tools > n8n Workflow Trigger > N8N_API_KEY"
            )

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self.valves.N8N_URL}/api/v1/workflows",
                    headers=self._headers(),
                )
                resp.raise_for_status()
                data = resp.json()

            workflows = data.get("data", [])
            if not workflows:
                return "No workflows found. Create your first workflow in n8n."

            lines = ["## n8n Workflows\n"]
            for wf in workflows:
                status = "✅ Active" if wf.get("active") else "⏸ Inactive"
                name = wf.get("name", "Unnamed")
                wf_id = wf.get("id", "")
                lines.append(f"**{name}** [{status}] (id: {wf_id})")

            return "\n".join(lines)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                return "Invalid n8n API key. Check the key in tool settings."
            return f"n8n API error: HTTP {e.response.status_code}"
        except Exception as e:
            return f"n8n list error: {str(e)}"
