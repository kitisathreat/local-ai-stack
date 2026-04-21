"""
Tests for pure utility functions in backend/admin.py.

Covers the config-patching helpers (_patch_vram, _patch_router, _patch_auth,
_patch_tiers, _set_deep, _load_yaml, _atomic_write_yaml) and the role-gate
helpers (_admin_emails, is_admin_email). These are all pure or nearly-pure
functions with no live HTTP or database calls.
"""
from __future__ import annotations

import os
import sys
import types
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# backend.auth imports jose which may panic in environments with a broken
# cryptography/cffi build. Attempt the real import first; only stub on failure.
if "backend.auth" not in sys.modules:
    try:
        import backend.auth  # noqa: F401
    except BaseException:
        _auth_stub = types.ModuleType("backend.auth")
        _auth_stub.get_current_user = MagicMock()
        _auth_stub.optional_user = MagicMock()
        _auth_stub.current_user = MagicMock()
        _auth_stub.require_admin = MagicMock()
        sys.modules["backend.auth"] = _auth_stub

from backend.admin import (
    _admin_emails,
    _atomic_write_yaml,
    _load_yaml,
    _patch_auth,
    _patch_router,
    _patch_vram,
    _set_deep,
    is_admin_email,
)


# ═══════════════════════════════════════════════════════════════════════════════
# _admin_emails / is_admin_email
# ═══════════════════════════════════════════════════════════════════════════════

class TestAdminEmails:

    def test_parses_single_email(self, monkeypatch):
        monkeypatch.setenv("ADMIN_EMAILS", "admin@example.com")
        assert "admin@example.com" in _admin_emails()

    def test_parses_multiple_emails(self, monkeypatch):
        monkeypatch.setenv("ADMIN_EMAILS", "a@x.io, b@x.io, c@x.io")
        emails = _admin_emails()
        assert emails == {"a@x.io", "b@x.io", "c@x.io"}

    def test_empty_env_returns_empty_set(self, monkeypatch):
        monkeypatch.delenv("ADMIN_EMAILS", raising=False)
        assert _admin_emails() == set()

    def test_lowercases_addresses(self, monkeypatch):
        monkeypatch.setenv("ADMIN_EMAILS", "ADMIN@EXAMPLE.COM")
        assert "admin@example.com" in _admin_emails()

    def test_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("ADMIN_EMAILS", "  admin@x.io  ")
        assert "admin@x.io" in _admin_emails()

    def test_is_admin_email_true_for_match(self, monkeypatch):
        monkeypatch.setenv("ADMIN_EMAILS", "admin@x.io")
        assert is_admin_email("admin@x.io") is True

    def test_is_admin_email_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("ADMIN_EMAILS", "admin@x.io")
        assert is_admin_email("ADMIN@X.IO") is True

    def test_is_admin_email_false_for_mismatch(self, monkeypatch):
        monkeypatch.setenv("ADMIN_EMAILS", "admin@x.io")
        assert is_admin_email("user@x.io") is False

    def test_is_admin_email_false_for_none(self, monkeypatch):
        monkeypatch.setenv("ADMIN_EMAILS", "admin@x.io")
        assert is_admin_email(None) is False

    def test_is_admin_email_false_when_no_admins_configured(self, monkeypatch):
        monkeypatch.delenv("ADMIN_EMAILS", raising=False)
        assert is_admin_email("admin@x.io") is False


# ═══════════════════════════════════════════════════════════════════════════════
# _set_deep
# ═══════════════════════════════════════════════════════════════════════════════

class TestSetDeep:

    def test_sets_top_level_key(self):
        d = {}
        _set_deep(d, ["key"], "value")
        assert d == {"key": "value"}

    def test_creates_nested_dicts(self):
        d = {}
        _set_deep(d, ["a", "b", "c"], 42)
        assert d == {"a": {"b": {"c": 42}}}

    def test_overwrites_existing_value(self):
        d = {"a": {"b": "old"}}
        _set_deep(d, ["a", "b"], "new")
        assert d["a"]["b"] == "new"

    def test_replaces_non_dict_intermediate_with_dict(self):
        d = {"a": "not_a_dict"}
        _set_deep(d, ["a", "b"], "val")
        assert d["a"] == {"b": "val"}

    def test_single_element_path(self):
        d = {"x": 1}
        _set_deep(d, ["x"], 99)
        assert d["x"] == 99


# ═══════════════════════════════════════════════════════════════════════════════
# _load_yaml / _atomic_write_yaml
# ═══════════════════════════════════════════════════════════════════════════════

class TestLoadAndWriteYaml:

    def test_load_yaml_reads_file(self, tmp_path):
        p = tmp_path / "test.yaml"
        p.write_text("key: value\nnested:\n  x: 1\n", encoding="utf-8")
        data = _load_yaml(p)
        assert data["key"] == "value"
        assert data["nested"]["x"] == 1

    def test_load_yaml_missing_file_returns_empty_dict(self, tmp_path):
        p = tmp_path / "nonexistent.yaml"
        assert _load_yaml(p) == {}

    def test_load_yaml_empty_file_returns_empty_dict(self, tmp_path):
        p = tmp_path / "empty.yaml"
        p.write_text("", encoding="utf-8")
        assert _load_yaml(p) == {}

    def test_atomic_write_yaml_creates_file(self, tmp_path):
        p = tmp_path / "out.yaml"
        _atomic_write_yaml(p, {"a": 1, "b": [2, 3]})
        assert p.exists()
        loaded = yaml.safe_load(p.read_text())
        assert loaded == {"a": 1, "b": [2, 3]}

    def test_atomic_write_yaml_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "nested" / "dir" / "out.yaml"
        _atomic_write_yaml(p, {"x": "y"})
        assert p.exists()

    def test_atomic_write_yaml_overwrites_existing(self, tmp_path):
        p = tmp_path / "out.yaml"
        p.write_text("old: content\n")
        _atomic_write_yaml(p, {"new": "content"})
        loaded = yaml.safe_load(p.read_text())
        assert "old" not in loaded
        assert loaded["new"] == "content"


# ═══════════════════════════════════════════════════════════════════════════════
# _patch_vram
# ═══════════════════════════════════════════════════════════════════════════════

class TestPatchVram:

    def test_patches_total_vram_gb(self):
        doc = {}
        changes = _patch_vram({"total_vram_gb": 48.0}, doc)
        assert doc["total_vram_gb"] == 48.0
        assert "vram.total_vram_gb" in changes

    def test_patches_headroom_gb(self):
        doc = {}
        _patch_vram({"headroom_gb": 2.5}, doc)
        assert doc["headroom_gb"] == 2.5

    def test_patches_eviction_policy(self):
        doc = {}
        _patch_vram({"eviction": {"policy": "lru"}}, doc)
        assert doc["eviction"]["policy"] == "lru"

    def test_patches_eviction_pin_flags(self):
        doc = {}
        _patch_vram({"eviction": {"pin_vision": True, "pin_orchestrator": False}}, doc)
        assert doc["eviction"]["pin_vision"] is True
        assert doc["eviction"]["pin_orchestrator"] is False

    def test_patches_ollama_keep_alive(self):
        doc = {}
        _patch_vram({"ollama": {"keep_alive_default": "1h", "keep_alive_pinned": -1}}, doc)
        assert doc["ollama"]["keep_alive_default"] == "1h"
        assert doc["ollama"]["keep_alive_pinned"] == -1

    def test_queue_max_depth_clamped(self):
        doc = {}
        _patch_vram({"queue": {"max_depth_per_tier": 99999}}, doc)
        assert doc["queue"]["max_depth_per_tier"] == 1000  # clamped to 1000

    def test_queue_max_wait_clamped(self):
        doc = {}
        _patch_vram({"queue": {"max_wait_sec": 9999}}, doc)
        assert doc["queue"]["max_wait_sec"] == 600  # clamped to 600

    def test_empty_patch_makes_no_changes(self):
        doc = {}
        changes = _patch_vram({}, doc)
        assert changes == []
        assert doc == {}

    def test_none_values_skipped(self):
        doc = {}
        changes = _patch_vram({"total_vram_gb": None}, doc)
        assert changes == []

    def test_returns_list_of_changed_paths(self):
        doc = {}
        changes = _patch_vram({"total_vram_gb": 24.0, "headroom_gb": 1.5}, doc)
        assert "vram.total_vram_gb" in changes
        assert "vram.headroom_gb" in changes


# ═══════════════════════════════════════════════════════════════════════════════
# _patch_router
# ═══════════════════════════════════════════════════════════════════════════════

class TestPatchRouter:

    def test_patches_multi_agent_max_workers(self):
        doc = {}
        _patch_router({"multi_agent": {"max_workers": 6}}, doc)
        assert doc["multi_agent"]["max_workers"] == 6

    def test_max_workers_clamped_to_8(self):
        doc = {}
        _patch_router({"multi_agent": {"max_workers": 99}}, doc)
        assert doc["multi_agent"]["max_workers"] == 8

    def test_max_workers_minimum_1(self):
        doc = {}
        _patch_router({"multi_agent": {"max_workers": -5}}, doc)
        assert doc["multi_agent"]["max_workers"] == 1

    def test_patches_worker_tier(self):
        doc = {}
        _patch_router({"multi_agent": {"worker_tier": "coding"}}, doc)
        assert doc["multi_agent"]["worker_tier"] == "coding"

    def test_invalid_interaction_mode_coerced_to_independent(self):
        doc = {}
        _patch_router({"multi_agent": {"interaction_mode": "roundtable"}}, doc)
        assert doc["multi_agent"]["interaction_mode"] == "independent"

    def test_collaborative_mode_accepted(self):
        doc = {}
        _patch_router({"multi_agent": {"interaction_mode": "collaborative"}}, doc)
        assert doc["multi_agent"]["interaction_mode"] == "collaborative"

    def test_interaction_rounds_clamped_to_4(self):
        doc = {}
        _patch_router({"multi_agent": {"interaction_rounds": 100}}, doc)
        assert doc["multi_agent"]["interaction_rounds"] == 4

    def test_patches_auto_thinking_enable_signals(self):
        doc = {}
        _patch_router({
            "auto_thinking_signals": {
                "enable_when_any": [{"regex": r"\bprove\b"}],
            }
        }, doc)
        assert doc["auto_thinking_signals"]["enable_when_any"] == [{"regex": r"\bprove\b"}]

    def test_strips_signal_entries_without_regex(self):
        doc = {}
        _patch_router({
            "auto_thinking_signals": {
                "enable_when_any": [{"regex": r"\bfoo\b"}, {"no_regex": "here"}],
            }
        }, doc)
        # Only entries with "regex" key survive
        assert len(doc["auto_thinking_signals"]["enable_when_any"]) == 1

    def test_empty_patch_returns_no_changes(self):
        doc = {}
        changes = _patch_router({}, doc)
        assert changes == []


# ═══════════════════════════════════════════════════════════════════════════════
# _patch_auth
# ═══════════════════════════════════════════════════════════════════════════════

class TestPatchAuth:

    def test_patches_magic_link_expiry(self):
        doc = {}
        _patch_auth({"magic_link_expiry_minutes": 30}, doc)
        assert doc["magic_link"]["expiry_minutes"] == 30

    def test_patches_allowed_email_domains_as_list(self):
        doc = {}
        _patch_auth({"allowed_email_domains": ["example.com", "corp.io"]}, doc)
        assert "example.com" in doc["allowed_email_domains"]

    def test_patches_allowed_email_domains_as_comma_string(self):
        doc = {}
        _patch_auth({"allowed_email_domains": "example.com, corp.io"}, doc)
        assert "example.com" in doc["allowed_email_domains"]
        assert "corp.io" in doc["allowed_email_domains"]

    def test_domains_lowercased(self):
        doc = {}
        _patch_auth({"allowed_email_domains": ["EXAMPLE.COM"]}, doc)
        assert "example.com" in doc["allowed_email_domains"]

    def test_patches_requests_per_minute_per_user(self):
        doc = {}
        _patch_auth({"rate_limits": {"requests_per_minute_per_user": 60}}, doc)
        assert doc["rate_limits"]["requests_per_minute_per_user"] == 60

    def test_requests_per_minute_clamped_to_10000(self):
        doc = {}
        _patch_auth({"rate_limits": {"requests_per_minute_per_user": 999999}}, doc)
        assert doc["rate_limits"]["requests_per_minute_per_user"] == 10_000

    def test_requests_per_day_minimum_0(self):
        doc = {}
        _patch_auth({"rate_limits": {"requests_per_day_per_user": -10}}, doc)
        assert doc["rate_limits"]["requests_per_day_per_user"] == 0

    def test_patches_cookie_ttl_days(self):
        doc = {}
        _patch_auth({"session": {"cookie_ttl_days": 7}}, doc)
        assert doc["session"]["cookie_ttl_days"] == 7

    def test_empty_patch_returns_no_changes(self):
        doc = {}
        changes = _patch_auth({}, doc)
        assert changes == []
