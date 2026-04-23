"""Dry-run mode for pull_missing_* helpers.

Proves that ``dry_run=True`` walks the same tier-decision logic as a real
pull — iterating resolved tiers, probing local availability — but never
invokes ``subprocess.run`` (for Ollama) or ``hf_hub_download`` (for
Hugging Face). CI relies on this to verify the pull machinery is wired
without downloading tens of GBs of weights.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from backend import model_resolver
from backend.model_resolver import (
    Resolved,
    ResolveResult,
    pull_missing_hf_files,
    pull_missing_ollama_tags,
)


def _make_result(**tiers: Resolved) -> ResolveResult:
    return ResolveResult(resolved=dict(tiers))


def test_ollama_dry_run_enumerates_without_subprocess(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError(f"subprocess.run called in dry-run: {args}")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(model_resolver, "_probe_ollama_local", lambda _tag: False)

    result = _make_result(
        fast=Resolved(tier="fast", source="ollama", identifier="llama3.2:1b"),
        big=Resolved(tier="big", source="ollama", identifier="llama3.1:70b"),
    )

    pulled = pull_missing_ollama_tags(result, dry_run=True)

    assert sorted(pulled) == ["llama3.1:70b", "llama3.2:1b"]


def test_ollama_dry_run_skips_already_local_tiers(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("no subprocess"))
    # Mark "big" as already pulled locally.
    monkeypatch.setattr(
        model_resolver,
        "_probe_ollama_local",
        lambda tag: tag == "llama3.1:70b",
    )

    result = _make_result(
        fast=Resolved(tier="fast", source="ollama", identifier="llama3.2:1b"),
        big=Resolved(tier="big", source="ollama", identifier="llama3.1:70b"),
    )

    pulled = pull_missing_ollama_tags(result, dry_run=True)

    assert pulled == ["llama3.2:1b"]


def test_ollama_dry_run_skips_errored_tiers(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("no subprocess"))
    monkeypatch.setattr(model_resolver, "_probe_ollama_local", lambda _tag: False)

    result = _make_result(
        fast=Resolved(tier="fast", source="ollama", identifier="llama3.2:1b"),
        broken=Resolved(
            tier="broken", source="ollama", identifier="llama3.1:70b",
            error="registry poll failed",
        ),
    )

    pulled = pull_missing_ollama_tags(result, dry_run=True)

    assert pulled == ["llama3.2:1b"]


def test_hf_dry_run_enumerates_without_downloading(monkeypatch, tmp_path):
    def _boom(*args, **kwargs):
        raise AssertionError(f"hf_hub_download called in dry-run: {args} {kwargs}")

    fake_hf = type(sys)("huggingface_hub")
    fake_hf.hf_hub_download = _boom
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hf)

    result = _make_result(
        vision=Resolved(
            tier="vision", source="huggingface",
            identifier="org/repo/model.gguf", revision="abc123",
        ),
        ignore_ollama=Resolved(
            tier="ignore_ollama", source="ollama", identifier="llama3.2:1b",
        ),
    )

    pulled = pull_missing_hf_files(result, model_dir=tmp_path, dry_run=True)

    assert pulled == ["vision"]


def test_hf_dry_run_skips_existing_files(monkeypatch, tmp_path):
    fake_hf = type(sys)("huggingface_hub")
    fake_hf.hf_hub_download = lambda *a, **k: pytest.fail("no download")
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hf)

    # Pre-create the file so the helper sees it as already on disk.
    (tmp_path / "model.gguf").write_bytes(b"x")

    result = _make_result(
        vision=Resolved(
            tier="vision", source="huggingface",
            identifier="org/repo/model.gguf", revision="abc123",
        ),
    )

    pulled = pull_missing_hf_files(result, model_dir=tmp_path, dry_run=True)

    assert pulled == []
