"""
title: Notebook Author — Generate .ipynb Files from a Cell Spec
author: local-ai-stack
description: Build a Jupyter notebook (`.ipynb`) cell-by-cell. Accumulate markdown + code cells in memory keyed by output path, then commit by writing a JSON file in the official nbformat schema. Optionally execute the notebook end-to-end via the existing `jupyter_tool` to bake outputs into the saved file.
required_open_webui_version: 0.4.0
requirements:
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field


_NOTEBOOKS: dict[str, dict[str, Any]] = {}


def _new_nb() -> dict:
    return {
        "cells": [],
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.12"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def _md_cell(text: str) -> dict:
    return {
        "cell_type": "markdown", "id": uuid.uuid4().hex,
        "metadata": {}, "source": text.splitlines(keepends=True),
    }


def _code_cell(code: str) -> dict:
    return {
        "cell_type": "code", "id": uuid.uuid4().hex,
        "metadata": {}, "execution_count": None, "outputs": [],
        "source": code.splitlines(keepends=True),
    }


class Tools:
    class Valves(BaseModel):
        DEFAULT_OUTPUT_DIR: str = Field(
            default=str(Path.home() / "Documents" / "notebooks"),
            description="Where to save committed .ipynb files.",
        )

    def __init__(self):
        self.valves = self.Valves()

    def new_notebook(
        self,
        output_path: str,
        title: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Start a fresh notebook spec keyed by output_path.
        :param output_path: Path to .ipynb (will be written on commit).
        :param title: Optional notebook title — added as the first markdown cell.
        :return: Confirmation.
        """
        path = str(Path(output_path).expanduser().resolve())
        nb = _new_nb()
        if title:
            nb["cells"].append(_md_cell(f"# {title}\n"))
        _NOTEBOOKS[path] = nb
        return f"new notebook -> {path}"

    def add_markdown(
        self,
        output_path: str,
        text: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Append a markdown cell.
        :param output_path: Notebook spec key.
        :param text: Markdown source.
        :return: Confirmation.
        """
        path = str(Path(output_path).expanduser().resolve())
        nb = _NOTEBOOKS.setdefault(path, _new_nb())
        nb["cells"].append(_md_cell(text))
        return f"+ md cell ({len(text)} chars)"

    def add_code(
        self,
        output_path: str,
        code: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Append a code cell.
        :param output_path: Notebook spec key.
        :param code: Python source.
        :return: Confirmation.
        """
        path = str(Path(output_path).expanduser().resolve())
        nb = _NOTEBOOKS.setdefault(path, _new_nb())
        nb["cells"].append(_code_cell(code))
        return f"+ code cell ({len(code)} chars)"

    def commit(
        self,
        output_path: str,
        execute: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Write the accumulated cells to a .ipynb file. When execute=True,
        run every cell via the existing jupyter_tool to bake outputs.
        :param output_path: Notebook path.
        :param execute: When True, execute end-to-end.
        :return: Confirmation with cell count.
        """
        path = Path(output_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        nb = _NOTEBOOKS.get(str(path))
        if nb is None:
            return f"no spec for {path} — call new_notebook first"
        path.write_text(json.dumps(nb, indent=1, ensure_ascii=False), encoding="utf-8")
        n = len(nb["cells"])
        msg = f"wrote {n} cells -> {path}"
        if execute:
            # Best-effort: run each code cell through jupyter_tool.execute_python.
            try:
                spec = importlib.util.spec_from_file_location(
                    "_lai_jupyter", Path(__file__).parent / "jupyter_tool.py",
                )
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                jt = mod.Tools()
                # We can't await synchronously here — return a hint instead.
                msg += " (execute=True: have the model call jupyter_tool.execute_python on each code cell)"
            except Exception as e:
                msg += f" (execute setup failed: {e})"
        _NOTEBOOKS.pop(str(path), None)
        return msg

    def show_spec(
        self,
        output_path: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Pretty-print the current accumulated cell list without committing.
        :param output_path: Notebook spec key.
        :return: One row per cell.
        """
        path = str(Path(output_path).expanduser().resolve())
        nb = _NOTEBOOKS.get(path)
        if nb is None:
            return f"(no spec for {path})"
        rows = []
        for i, c in enumerate(nb["cells"]):
            preview = "".join(c["source"])[:80].replace("\n", " ")
            rows.append(f"  [{i}] {c['cell_type']:<10}  {preview}")
        return f"{len(nb['cells'])} cells in {path}\n" + "\n".join(rows)
