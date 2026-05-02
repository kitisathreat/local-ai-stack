"""
title: Repo Scaffolder — Bootstrap a Project Skeleton
author: local-ai-stack
description: Generate a complete project skeleton from a template (FastAPI, CLI, library, Next.js, Rust binary, Go service). Each template lays out the canonical directory structure with starter source, tests, CI workflow, README, .gitignore, and dependency manifest. The model can then call `filesystem.write_text` to layer custom logic on top, and `app_launcher` to open VS Code.
required_open_webui_version: 0.4.0
requirements:
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Any, Optional

from pydantic import BaseModel, Field


_TEMPLATES = {
    "python_cli": {
        "files": {
            "pyproject.toml": dedent('''\
                [project]
                name = "{name}"
                version = "0.1.0"
                description = "{description}"
                authors = [{{name = "{author}"}}]
                requires-python = ">=3.11"
                dependencies = ["click"]

                [project.scripts]
                {name} = "{name}.cli:main"

                [build-system]
                requires = ["hatchling"]
                build-backend = "hatchling.build"
                '''),
            "{name}/__init__.py": '"""{description}"""\n',
            "{name}/cli.py": dedent('''\
                import click

                @click.command()
                @click.option("--verbose", is_flag=True)
                def main(verbose: bool):
                    """{description}"""
                    if verbose:
                        click.echo("verbose on")
                    click.echo("hello from {name}")

                if __name__ == "__main__":
                    main()
                '''),
            "tests/test_cli.py": dedent('''\
                from click.testing import CliRunner
                from {name}.cli import main

                def test_main_says_hello():
                    r = CliRunner().invoke(main, [])
                    assert r.exit_code == 0
                    assert "hello" in r.output
                '''),
            "README.md": "# {name}\n\n{description}\n\n## Install\n\n```bash\npip install -e .\n```\n",
            ".gitignore": "__pycache__/\n*.pyc\n.venv/\nbuild/\ndist/\n*.egg-info/\n.pytest_cache/\n",
            ".github/workflows/ci.yml": dedent('''\
                name: CI
                on: [push, pull_request]
                jobs:
                  test:
                    runs-on: ubuntu-latest
                    steps:
                      - uses: actions/checkout@v4
                      - uses: actions/setup-python@v5
                        with: {{python-version: "3.12"}}
                      - run: pip install -e . pytest
                      - run: pytest
                '''),
        },
    },

    "fastapi": {
        "files": {
            "pyproject.toml": dedent('''\
                [project]
                name = "{name}"
                version = "0.1.0"
                description = "{description}"
                authors = [{{name = "{author}"}}]
                requires-python = ">=3.11"
                dependencies = ["fastapi", "uvicorn[standard]", "pydantic"]

                [build-system]
                requires = ["hatchling"]
                build-backend = "hatchling.build"
                '''),
            "{name}/__init__.py": "",
            "{name}/main.py": dedent('''\
                from fastapi import FastAPI

                app = FastAPI(title="{name}")

                @app.get("/healthz")
                def healthz():
                    return {{"status": "ok"}}

                @app.get("/")
                def root():
                    return {{"service": "{name}", "description": "{description}"}}
                '''),
            "tests/test_health.py": dedent('''\
                from fastapi.testclient import TestClient
                from {name}.main import app

                def test_healthz():
                    r = TestClient(app).get("/healthz")
                    assert r.status_code == 200
                    assert r.json() == {{"status": "ok"}}
                '''),
            "Dockerfile": dedent('''\
                FROM python:3.12-slim
                WORKDIR /app
                COPY . .
                RUN pip install -e .
                EXPOSE 8000
                CMD ["uvicorn", "{name}.main:app", "--host", "0.0.0.0", "--port", "8000"]
                '''),
            "README.md": "# {name}\n\n{description}\n\n## Run\n\n```bash\npip install -e .\nuvicorn {name}.main:app --reload\n```\n",
            ".gitignore": "__pycache__/\n*.pyc\n.venv/\nbuild/\ndist/\n*.egg-info/\n.pytest_cache/\n",
            ".github/workflows/ci.yml": dedent('''\
                name: CI
                on: [push, pull_request]
                jobs:
                  test:
                    runs-on: ubuntu-latest
                    steps:
                      - uses: actions/checkout@v4
                      - uses: actions/setup-python@v5
                        with: {{python-version: "3.12"}}
                      - run: pip install -e . pytest httpx
                      - run: pytest
                '''),
        },
    },

    "python_library": {
        "files": {
            "pyproject.toml": dedent('''\
                [project]
                name = "{name}"
                version = "0.1.0"
                description = "{description}"
                authors = [{{name = "{author}"}}]
                requires-python = ">=3.11"
                dependencies = []

                [build-system]
                requires = ["hatchling"]
                build-backend = "hatchling.build"
                '''),
            "{name}/__init__.py": '"""{description}"""\nfrom .core import hello\n',
            "{name}/core.py": dedent('''\
                def hello(name: str = "world") -> str:
                    """Return a greeting. Replace with your library logic."""
                    return f"hello, {{name}}"
                '''),
            "tests/test_core.py": dedent('''\
                from {name} import hello

                def test_hello_default():
                    assert hello() == "hello, world"
                def test_hello_custom():
                    assert hello("kit") == "hello, kit"
                '''),
            "README.md": "# {name}\n\n{description}\n",
            ".gitignore": "__pycache__/\n*.pyc\n.venv/\nbuild/\ndist/\n*.egg-info/\n.pytest_cache/\n",
        },
    },

    "rust_bin": {
        "files": {
            "Cargo.toml": dedent('''\
                [package]
                name = "{name}"
                version = "0.1.0"
                edition = "2021"
                description = "{description}"
                authors = ["{author}"]

                [dependencies]
                clap = {{ version = "4", features = ["derive"] }}
                '''),
            "src/main.rs": dedent('''\
                use clap::Parser;

                /// {description}
                #[derive(Parser, Debug)]
                struct Args {{
                    #[arg(short, long, default_value = "world")]
                    name: String,
                }}

                fn main() {{
                    let args = Args::parse();
                    println!("hello, {{}}", args.name);
                }}
                '''),
            "README.md": "# {name}\n\n{description}\n",
            ".gitignore": "/target\n",
        },
    },

    "go_service": {
        "files": {
            "go.mod": "module {name}\n\ngo 1.22\n",
            "main.go": dedent('''\
                package main

                import (
                    "fmt"
                    "log"
                    "net/http"
                )

                func main() {{
                    http.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {{
                        fmt.Fprintln(w, `{{"status":"ok"}}`)
                    }})
                    log.Println("{{name}} listening on :8080")
                    log.Fatal(http.ListenAndServe(":8080", nil))
                }}
                ''').replace("{{name}}", "{name}"),
            "README.md": "# {name}\n\n{description}\n",
            ".gitignore": "*.exe\n*.test\n*.out\n",
        },
    },
}


class Tools:
    class Valves(BaseModel):
        DEFAULT_AUTHOR: str = Field(default="local-ai-stack")

    def __init__(self):
        self.valves = self.Valves()

    def list_templates(self, __user__: Optional[dict] = None) -> str:
        """
        Show available scaffolds.
        :return: Newline-delimited template names + descriptions.
        """
        return "\n".join([
            "python_cli       — click-based CLI with pyproject.toml + tests + CI",
            "fastapi          — FastAPI service with /healthz, Dockerfile, tests + CI",
            "python_library   — minimal pip-installable Python library + tests",
            "rust_bin         — Rust binary using clap + Cargo.toml",
            "go_service       — Go HTTP service on :8080",
        ])

    def scaffold(
        self,
        template: str,
        name: str,
        directory: str,
        description: str = "",
        author: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Lay down the chosen scaffold under `<directory>/<name>/`.
        :param template: Template id (see list_templates).
        :param name: Project name (used as the package/binary name).
        :param directory: Parent directory.
        :param description: Free-text description for the README and metadata.
        :param author: Author name. Empty = DEFAULT_AUTHOR.
        :return: Per-file action log.
        """
        tpl = _TEMPLATES.get(template)
        if not tpl:
            return f"unknown template: {template}. Try: {sorted(_TEMPLATES)}"
        root = Path(directory).expanduser().resolve() / name
        if root.exists() and any(root.iterdir()):
            return f"refusing to scaffold into non-empty {root}"
        root.mkdir(parents=True, exist_ok=True)
        ctx = {
            "name": name,
            "description": description or f"{name}: a new {template} project",
            "author": author or self.valves.DEFAULT_AUTHOR,
        }
        log = []
        for tmpl_path, tmpl_text in tpl["files"].items():
            try:
                rel_path = tmpl_path.format(**ctx)
                content = tmpl_text.format(**ctx)
            except (KeyError, IndexError) as e:
                log.append(f"!! template placeholder error in {tmpl_path}: {e}")
                continue
            dest = root / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
            log.append(f"+ {dest.relative_to(root)}")
        return f"# Scaffolded {template} -> {root}\n" + "\n".join(log)
