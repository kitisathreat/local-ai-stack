"""Tests for backend.model_resolver, covering the HF-pull path
and the offline fallback behaviour. Ollama paths were removed in the
llama.cpp migration."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


# ── Offline resolution (existing, baseline) ────────────────────────────

def test_offline_falls_back_to_pinned(tmp_path, monkeypatch):
    monkeypatch.setenv("LAI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OFFLINE", "1")
    # Keep config pointing at the real model-sources.yaml.
    monkeypatch.setenv("LAI_CONFIG_DIR", str(ROOT / "config"))
    import importlib
    from backend import model_resolver
    importlib.reload(model_resolver)
    result = model_resolver.resolve(force=True, offline=True)
    assert result.offline is True
    assert result.resolved, "expected at least one tier"
    for tier, info in result.resolved.items():
        assert info.origin == "pinned"


# ── pull_missing_hf_files ──────────────────────────────────────────────

def _fake_resolve_result(tmp_path, filename="test.gguf", rev="abc1234"):
    from backend.model_resolver import Resolved, ResolveResult
    r = Resolved(
        tier="vision",
        source="huggingface",
        repo="Qwen/Fake",
        filename=filename,
        revision=rev,
        origin="latest",
    )
    return ResolveResult(resolved={"vision": r}, cached=False, offline=False)


def test_pull_missing_skips_non_hf_tiers(tmp_path, monkeypatch):
    monkeypatch.setenv("LAI_DATA_DIR", str(tmp_path))
    import importlib
    from backend import model_resolver
    importlib.reload(model_resolver)
    from backend.model_resolver import Resolved, ResolveResult, pull_missing_hf_files

    result = ResolveResult(
        resolved={"fast": Resolved(
            tier="fast", source="something-else", repo="qwen3.5", filename="9b.gguf",
        )},
        cached=False, offline=False,
    )
    pulled = pull_missing_hf_files(result)
    assert pulled == []


def test_pull_missing_downloads_vision(tmp_path, monkeypatch):
    monkeypatch.setenv("LAI_DATA_DIR", str(tmp_path))
    import importlib
    from backend import model_resolver
    importlib.reload(model_resolver)
    from backend.model_resolver import pull_missing_hf_files

    calls = []

    def _fake_download(repo_id, filename, revision, local_dir, token):
        calls.append({
            "repo_id": repo_id, "filename": filename,
            "revision": revision, "local_dir": local_dir,
        })
        target = Path(local_dir) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake gguf")
        return str(target)

    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", _fake_download)

    result = _fake_resolve_result(tmp_path)
    pulled = pull_missing_hf_files(result)

    assert pulled == ["vision"]
    assert calls and calls[0]["repo_id"] == "Qwen/Fake"
    assert calls[0]["filename"] == "test.gguf"
    assert (tmp_path / "models" / "vision.gguf").exists()


def test_pull_missing_tolerates_download_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("LAI_DATA_DIR", str(tmp_path))
    import importlib
    from backend import model_resolver
    importlib.reload(model_resolver)
    from backend.model_resolver import pull_missing_hf_files

    def _raise(*a, **k):
        raise RuntimeError("network down")

    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", _raise)

    result = _fake_resolve_result(tmp_path)
    # Should not raise; returns empty list and leaves vision.gguf absent.
    pulled = pull_missing_hf_files(result)
    assert pulled == []
    assert not (tmp_path / "models" / "vision.gguf").exists()


def test_pull_missing_ignores_tiers_without_filename(tmp_path, monkeypatch):
    """If filename is empty (resolver emitted incomplete data), skip quietly."""
    monkeypatch.setenv("LAI_DATA_DIR", str(tmp_path))
    import importlib
    from backend import model_resolver
    importlib.reload(model_resolver)
    from backend.model_resolver import Resolved, ResolveResult, pull_missing_hf_files

    result = ResolveResult(
        resolved={"bad": Resolved(
            tier="bad", source="huggingface", repo="Qwen/OnlyRepo", filename="",
        )},
        cached=False, offline=False,
    )
    pulled = pull_missing_hf_files(result)
    assert pulled == []
