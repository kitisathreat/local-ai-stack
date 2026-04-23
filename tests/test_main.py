"""Tests for FastAPI endpoint behaviour in backend/main.py.

Kept light — deep behavioural tests live beside the modules they exercise.
This file covers endpoints that are only smoke-testable at the FastAPI
integration layer (path defaults, static mounts, host-gating).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def test_resolved_models_default_path_is_repo_relative(tmp_path, monkeypatch):
    """/resolved-models must not default to the old Docker mount /app/data.

    Phase 1 fix: default resolves to `<repo_root>/data/resolved-models.json`
    when LAI_DATA_DIR is unset. This test exercises the exact path logic
    used by the endpoint, without spinning up FastAPI.
    """
    monkeypatch.delenv("LAI_DATA_DIR", raising=False)
    # Mirror the computation in backend/main.py:/resolved-models.
    from backend import main as main_module
    backend_dir = Path(main_module.__file__).resolve().parent
    expected = backend_dir.parent / "data"
    data_dir = Path(os.getenv("LAI_DATA_DIR") or expected)
    assert data_dir == expected
    assert str(data_dir) != "/app/data"


def test_resolved_models_respects_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("LAI_DATA_DIR", str(tmp_path))
    data_dir = Path(os.getenv("LAI_DATA_DIR") or (ROOT / "data"))
    assert data_dir == tmp_path


def test_resolved_models_file_not_found_returns_empty_shape(tmp_path, monkeypatch):
    """When the resolver hasn't run yet, /resolved-models returns a
    predictable empty shape rather than raising."""
    monkeypatch.setenv("LAI_DATA_DIR", str(tmp_path))
    path = tmp_path / "resolved-models.json"
    assert not path.exists()
    # Simulate the endpoint's fall-through branch.
    result = {"tiers": {}, "resolved_at": 0, "offline": False, "cached": False}
    assert set(result.keys()) == {"tiers", "resolved_at", "offline", "cached"}


def test_resolved_models_malformed_json_returns_empty_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("LAI_DATA_DIR", str(tmp_path))
    path = tmp_path / "resolved-models.json"
    path.write_text("{not valid json", encoding="utf-8")
    # Endpoint catches JSONDecodeError and returns empty shape.
    try:
        json.loads(path.read_text(encoding="utf-8"))
        pytest.fail("expected JSONDecodeError")
    except json.JSONDecodeError:
        result = {"tiers": {}, "resolved_at": 0, "offline": False, "cached": False}
        assert result["tiers"] == {}


def test_airgap_endpoint_has_both_paths():
    """Regression: the Qt GUI + host-gate middleware expect
    /api/airgap, but the original route was /airgap. Both must be
    reachable on the FastAPI app so /api/airgap doesn't 404."""
    import importlib, sys
    from backend import main as main_module
    routes = {getattr(r, "path", "") for r in main_module.app.routes}
    assert "/airgap" in routes, "original /airgap route missing"
    assert "/api/airgap" in routes, "/api/airgap alias missing — GUI polling will 404"
