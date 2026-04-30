"""Dry-run mode for pull_missing_hf_files.

Proves that ``dry_run=True`` walks the same tier-decision logic as a real
pull — iterating resolved tiers and checking local file presence — but
never invokes ``hf_hub_download``. CI relies on this to verify the pull
machinery is wired without downloading tens of GBs of weights.
"""
from __future__ import annotations

import os
import sys

import pytest

from backend import model_resolver
from backend.model_resolver import (
    Resolved,
    ResolveResult,
    pull_missing_hf_files,
)


def _make_result(**tiers: Resolved) -> ResolveResult:
    return ResolveResult(resolved=dict(tiers))


def test_hf_dry_run_enumerates_without_downloading(monkeypatch, tmp_path):
    def _boom(*args, **kwargs):
        raise AssertionError(f"hf_hub_download called in dry-run: {args} {kwargs}")

    fake_hf = type(sys)("huggingface_hub")
    fake_hf.hf_hub_download = _boom
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hf)
    monkeypatch.setenv("LAI_DATA_DIR", str(tmp_path))

    result = _make_result(
        vision=Resolved(
            tier="vision", source="huggingface", repo="org/repo",
            filename="model.gguf", revision="abc123",
        ),
    )

    pulled = pull_missing_hf_files(result, dry_run=True)

    assert pulled == ["vision"]


def test_hf_dry_run_skips_existing_files(monkeypatch, tmp_path):
    fake_hf = type(sys)("huggingface_hub")
    fake_hf.hf_hub_download = lambda *a, **k: pytest.fail("no download")
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hf)
    monkeypatch.setenv("LAI_DATA_DIR", str(tmp_path))

    # Pre-create the canonical per-tier file so the helper sees it on disk.
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / "vision.gguf").write_bytes(b"x")

    result = _make_result(
        vision=Resolved(
            tier="vision", source="huggingface", repo="org/repo",
            filename="model.gguf", revision="abc123",
        ),
    )

    pulled = pull_missing_hf_files(result, dry_run=True)

    assert pulled == []


def test_hf_dry_run_skips_non_hf_sources(monkeypatch, tmp_path):
    fake_hf = type(sys)("huggingface_hub")
    fake_hf.hf_hub_download = lambda *a, **k: pytest.fail("no download")
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hf)
    monkeypatch.setenv("LAI_DATA_DIR", str(tmp_path))

    result = _make_result(
        unknown=Resolved(
            tier="unknown", source="something-else", repo="org/repo",
            filename="model.gguf",
        ),
    )

    pulled = pull_missing_hf_files(result, dry_run=True)

    assert pulled == []
