"""
title: Jupyter Code Executor — Run Python in the Connected Jupyter Kernel
author: local-ai-stack
description: Execute arbitrary Python code in the local Jupyter container and get back stdout, stderr, and any matplotlib/plotly figures as inline images. The Jupyter instance has pandas, numpy, scipy, matplotlib, scikit-learn, and statsmodels pre-installed. Use this for custom financial models, data analysis, machine learning experiments, or anything that doesn't fit a pre-built tool. Requires the Jupyter service to be running (included in docker-compose.yml).
required_open_webui_version: 0.4.0
requirements: httpx websockets
version: 1.0.0
licence: MIT
"""

import os
import asyncio
import base64
import json
import uuid
from typing import Callable, Any, Optional
from pydantic import BaseModel, Field

import httpx

try:
    import websockets
    HAS_WS = True
except ImportError:
    HAS_WS = False

JUPYTER_BASE = "http://jupyter:8888"
JUPYTER_TOKEN = "local-ai-stack-token"
WS_BASE = "ws://jupyter:8888"


class Tools:
    class Valves(BaseModel):
        JUPYTER_URL: str = Field(
            default="http://jupyter:8888",
            description="Jupyter server URL (internal Docker URL, e.g. http://jupyter:8888)",
        )
        JUPYTER_TOKEN: str = Field(
            default="local-ai-stack-token",
            description="Jupyter authentication token (set in JUPYTER_TOKEN env var in docker-compose.yml)",
        )
        EXECUTION_TIMEOUT: int = Field(
            default=60,
            description="Max seconds to wait for code execution (default 60)",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def execute_python(
        self,
        code: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Execute Python code in the Jupyter kernel and return stdout, errors, and any matplotlib figures as inline images.
        :param code: Python code to execute. Can use pandas, numpy, scipy, matplotlib, sklearn, statsmodels. For charts: use plt.show() or fig.savefig() — figures are auto-captured.
        :return: Code output including text, tables, and embedded chart images
        """
        if not HAS_WS:
            return "Error: `websockets` package not installed. Add `websockets` to requirements."

        base_url = self.valves.JUPYTER_URL.rstrip("/")
        token = self.valves.JUPYTER_TOKEN
        timeout = self.valves.EXECUTION_TIMEOUT
        ws_base = base_url.replace("http://", "ws://").replace("https://", "wss://")

        headers = {"Authorization": f"token {token}"}

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Starting Jupyter kernel...", "done": False}})

        # Create a kernel
        kernel_id = None
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(f"{base_url}/api/kernels", headers=headers,
                                         json={"name": "python3"})
                resp.raise_for_status()
                kernel_id = resp.json()["id"]
        except Exception as e:
            return f"Could not start Jupyter kernel: {e}\n\nMake sure the Jupyter service is running: `docker compose up -d jupyter`"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Executing code...", "done": False}})

        output_lines = []
        images = []

        try:
            ws_url = f"{ws_base}/api/kernels/{kernel_id}/channels?token={token}"

            async with websockets.connect(ws_url, ping_interval=None) as ws:
                msg_id = str(uuid.uuid4())
                execute_msg = {
                    "header": {
                        "msg_id": msg_id,
                        "msg_type": "execute_request",
                        "username": "local-ai-stack",
                        "session": str(uuid.uuid4()),
                        "version": "5.3",
                    },
                    "parent_header": {},
                    "metadata": {},
                    "content": {
                        "code": code,
                        "silent": False,
                        "store_history": False,
                        "user_expressions": {},
                        "allow_stdin": False,
                        "stop_on_error": True,
                    },
                    "channel": "shell",
                    "buffers": [],
                }
                await ws.send(json.dumps(execute_msg))

                deadline = asyncio.get_event_loop().time() + timeout
                execution_done = False

                while asyncio.get_event_loop().time() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    except asyncio.TimeoutError:
                        if execution_done:
                            break
                        continue

                    msg = json.loads(raw)
                    msg_type = msg.get("msg_type", "")
                    parent_msg_id = msg.get("parent_header", {}).get("msg_id", "")

                    if parent_msg_id != msg_id:
                        continue

                    content = msg.get("content", {})

                    if msg_type == "stream":
                        text = content.get("text", "")
                        if text.strip():
                            output_lines.append(text.rstrip())

                    elif msg_type == "execute_result":
                        data = content.get("data", {})
                        if "text/plain" in data:
                            output_lines.append(data["text/plain"])
                        if "text/html" in data:
                            html = data["text/html"]
                            # extract table text if short enough
                            import re
                            clean = re.sub(r"<[^>]+>", " ", html)
                            clean = re.sub(r"\s+", " ", clean).strip()
                            if len(clean) < 2000:
                                output_lines.append(clean)

                    elif msg_type == "display_data":
                        data = content.get("data", {})
                        if "image/png" in data:
                            images.append(data["image/png"])
                        elif "text/plain" in data:
                            output_lines.append(data["text/plain"])

                    elif msg_type == "error":
                        ename = content.get("ename", "Error")
                        evalue = content.get("evalue", "")
                        traceback = content.get("traceback", [])
                        clean_tb = []
                        for line in traceback:
                            clean_line = line
                            for code_str in ["\x1b[", "\033["]:
                                while code_str in clean_line:
                                    start = clean_line.find(code_str)
                                    end = clean_line.find("m", start)
                                    if end == -1:
                                        break
                                    clean_line = clean_line[:start] + clean_line[end+1:]
                            clean_tb.append(clean_line)
                        output_lines.append(f"**{ename}:** {evalue}")
                        if clean_tb:
                            output_lines.append("```\n" + "\n".join(clean_tb[-8:]) + "\n```")

                    elif msg_type == "execute_reply":
                        execution_done = True
                        status = content.get("status", "")
                        if status == "ok":
                            break
                        elif status == "error":
                            break

        except Exception as e:
            output_lines.append(f"WebSocket execution error: {e}")
        finally:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.delete(f"{base_url}/api/kernels/{kernel_id}", headers=headers)
            except Exception:
                pass

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        result_parts = []

        if output_lines:
            combined = "\n".join(output_lines)
            if len(combined) > 8000:
                combined = combined[:8000] + "\n... (output truncated)"
            result_parts.append(f"```\n{combined}\n```")

        for img_b64 in images[:5]:
            result_parts.append(f"![figure](data:image/png;base64,{img_b64})")

        if not result_parts:
            return "_Code executed successfully (no output)._"

        return "\n\n".join(result_parts)

    async def get_available_packages(
        self,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List the key Python packages available in the Jupyter kernel (pandas, numpy, scipy, matplotlib, sklearn, etc.).
        :return: Available packages with version numbers
        """
        code = """
import importlib
packages = [
    'numpy', 'pandas', 'scipy', 'matplotlib', 'sklearn', 'statsmodels',
    'seaborn', 'plotly', 'sympy', 'networkx', 'PIL', 'cv2',
    'torch', 'tensorflow', 'keras', 'xgboost', 'lightgbm',
    'openpyxl', 'xlrd', 'requests', 'httpx', 'bs4',
]
results = []
for pkg in packages:
    try:
        mod = importlib.import_module(pkg)
        ver = getattr(mod, '__version__', 'installed')
        results.append(f"✓ {pkg} {ver}")
    except ImportError:
        results.append(f"✗ {pkg} (not installed)")
print("\\n".join(results))
"""
        return await self.execute_python(code, __event_emitter__=__event_emitter__, __user__=__user__)
