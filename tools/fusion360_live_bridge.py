"""
title: Fusion 360 Live Bridge — Read State From a Running Fusion Session
author: local-ai-stack
description: When Fusion 360 is running with the IPC bridge add-in loaded, this tool talks to it over a local TCP socket to read live design state (component tree, sketches, parameters, materials) and to inject one-shot adsk.* Python snippets without writing a script-folder + restarting Fusion. The companion add-in is auto-installed via `fusion360.install_addin` the first time you call `ensure_bridge()`. Note: Fusion's official IPC API is still evolving — this tool talks a small JSON-RPC protocol the add-in implements itself.
required_open_webui_version: 0.4.0
requirements:
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import importlib.util
import json
import socket
from pathlib import Path
from textwrap import dedent
from typing import Any, Optional

from pydantic import BaseModel, Field


def _fusion_tool():
    spec = importlib.util.spec_from_file_location(
        "_lai_fusion_runner", Path(__file__).parent / "fusion360.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.Tools()


_BRIDGE_ADDIN_NAME = "lai_fusion_bridge"

# Add-in source — runs a tiny TCP JSON-RPC server inside Fusion. Each
# request is `{"op": "...", "args": {...}}`, response is JSON.
_BRIDGE_SOURCE = dedent('''
    import adsk.core, adsk.fusion, traceback, json, socket, threading

    PORT = 39713
    _server = None
    _stop = False

    def _eval_op(op, args):
        app = adsk.core.Application.get()
        des = adsk.fusion.Design.cast(app.activeProduct)
        if op == "ping":
            return {"ok": True, "version": app.version}
        if op == "active_doc":
            d = app.activeDocument
            return {"name": d.name if d else None, "type": str(d.documentType) if d else None}
        if op == "components":
            if not des: return {"error": "no active design"}
            out = []
            for occ in des.rootComponent.allOccurrences:
                out.append({
                    "name": occ.name, "component": occ.component.name,
                    "is_visible": occ.isVisible,
                })
            return {"components": out, "root": des.rootComponent.name}
        if op == "sketches":
            if not des: return {"error": "no active design"}
            out = []
            for sk in des.rootComponent.sketches:
                out.append({"name": sk.name, "is_valid": sk.isValid,
                            "profile_count": sk.profiles.count})
            return {"sketches": out}
        if op == "parameters":
            if not des: return {"error": "no active design"}
            out = []
            for p in des.allParameters:
                out.append({"name": p.name, "value": p.value, "expression": p.expression,
                            "unit": p.unit})
            return {"parameters": out}
        if op == "exec":
            ns = {"adsk": adsk, "app": app, "design": des}
            try:
                exec(args.get("code", ""), ns)
                return {"ok": True, "result": str(ns.get("__result__", ""))}
            except Exception:
                return {"error": traceback.format_exc()}
        return {"error": f"unknown op: {op}"}

    def _serve():
        global _stop
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", PORT))
        s.listen(4)
        s.settimeout(1.0)
        while not _stop:
            try:
                conn, _ = s.accept()
            except socket.timeout:
                continue
            try:
                buf = b""
                while True:
                    chunk = conn.recv(4096)
                    if not chunk: break
                    buf += chunk
                    if buf.endswith(b"\\n"): break
                req = json.loads(buf.decode("utf-8", "ignore") or "{}")
                resp = _eval_op(req.get("op"), req.get("args") or {})
                conn.sendall((json.dumps(resp) + "\\n").encode("utf-8"))
            except Exception:
                try: conn.sendall((json.dumps({"error": traceback.format_exc()}) + "\\n").encode())
                except: pass
            conn.close()

    def run(context):
        global _server, _stop
        _stop = False
        _server = threading.Thread(target=_serve, daemon=True)
        _server.start()
        ui = adsk.core.Application.get().userInterface
        ui.messageBox(f"lai_fusion_bridge listening on 127.0.0.1:{PORT}")

    def stop(context):
        global _stop
        _stop = True
''')


class Tools:
    class Valves(BaseModel):
        BRIDGE_HOST: str = Field(default="127.0.0.1")
        BRIDGE_PORT: int = Field(default=39713)
        TIMEOUT: float = Field(default=10.0)

    def __init__(self):
        self.valves = self.Valves()

    def _rpc(self, op: str, args: dict | None = None) -> dict:
        try:
            with socket.create_connection(
                (self.valves.BRIDGE_HOST, self.valves.BRIDGE_PORT),
                timeout=self.valves.TIMEOUT,
            ) as s:
                s.sendall((json.dumps({"op": op, "args": args or {}}) + "\n").encode("utf-8"))
                buf = b""
                while True:
                    chunk = s.recv(8192)
                    if not chunk: break
                    buf += chunk
                    if buf.endswith(b"\n"): break
            return json.loads(buf.decode("utf-8", "ignore") or "{}")
        except (ConnectionRefusedError, OSError) as e:
            return {"error": f"bridge not running: {e}"}
        except Exception as e:
            return {"error": str(e)}

    def ensure_bridge(self, autostart: bool = True, __user__: Optional[dict] = None) -> str:
        """
        Install (or refresh) the Fusion 360 add-in that runs the JSON-RPC
        bridge. Restart Fusion or load the add-in from Tools → Add-Ins to
        activate. With autostart=True the add-in's manifest is set to
        runOnStartup so subsequent Fusion launches expose the bridge
        automatically.
        :param autostart: When True, set runOnStartup on the manifest.
        :return: Confirmation with the install path.
        """
        runner = _fusion_tool()
        return runner.install_addin(
            name=_BRIDGE_ADDIN_NAME,
            full_source=_BRIDGE_SOURCE,
            autostart=autostart,
            description="Fusion 360 ↔ local-ai-stack JSON-RPC bridge.",
        )

    def ping(self, __user__: Optional[dict] = None) -> str:
        """
        Ping the running bridge. Returns Fusion's version string when alive.
        :return: JSON response.
        """
        return json.dumps(self._rpc("ping"))

    def active_document(self, __user__: Optional[dict] = None) -> str:
        """
        Return the active Fusion document name + type.
        :return: JSON response.
        """
        return json.dumps(self._rpc("active_doc"))

    def components(self, __user__: Optional[dict] = None) -> str:
        """
        List components in the active design (every occurrence in the
        root component).
        :return: JSON response.
        """
        return json.dumps(self._rpc("components"))

    def sketches(self, __user__: Optional[dict] = None) -> str:
        """
        List sketches in the active design (root component only).
        :return: JSON response.
        """
        return json.dumps(self._rpc("sketches"))

    def parameters(self, __user__: Optional[dict] = None) -> str:
        """
        List user/model parameters: name, expression, value, unit.
        :return: JSON response.
        """
        return json.dumps(self._rpc("parameters"))

    def execute(self, code: str, __user__: Optional[dict] = None) -> str:
        """
        Run an arbitrary adsk.* Python snippet in the bridge process.
        Set `__result__` in the snippet to return a string back.
        :param code: Python source to execute inside Fusion.
        :return: JSON response with `result` or `error` traceback.
        """
        return json.dumps(self._rpc("exec", {"code": code}))
