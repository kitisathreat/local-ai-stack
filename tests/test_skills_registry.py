"""Tests for backend/skills/registry.py — the SKILL.md loader."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


# ── Frontmatter parser ──────────────────────────────────────────────────────

def test_split_frontmatter_basic():
    from backend.skills.registry import _split_frontmatter
    text = "---\nname: foo\ntitle: Foo\n---\nbody body body\n"
    fm, body = _split_frontmatter(text)
    assert fm == {"name": "foo", "title": "Foo"}
    assert body.strip() == "body body body"


def test_split_frontmatter_missing_returns_whole_body():
    from backend.skills.registry import _split_frontmatter
    text = "no frontmatter here\nstill body\n"
    fm, body = _split_frontmatter(text)
    assert fm == {}
    assert body == text


def test_split_frontmatter_unclosed_returns_whole_body():
    from backend.skills.registry import _split_frontmatter
    text = "---\nname: foo\nno closing delimiter\n"
    fm, body = _split_frontmatter(text)
    assert fm == {}
    assert body == text


def test_split_frontmatter_handles_invalid_yaml():
    from backend.skills.registry import _split_frontmatter
    text = "---\nname: foo\n  bad indent: ]\n---\nbody\n"
    fm, body = _split_frontmatter(text)
    # Bad YAML should not crash; returns empty fm + raw body
    assert fm == {} or "name" not in fm or fm.get("name") == "foo"


# ── Discovery + loading ─────────────────────────────────────────────────────

def _write_skill(folder: Path, name: str, frontmatter: dict, body: str = "system body") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    fm_lines = "\n".join(f"{k}: {v}" for k, v in frontmatter.items())
    text = f"---\n{fm_lines}\n---\n{body}\n"
    (folder / "SKILL.md").write_text(text)
    return folder


def test_discovers_skill_pack(tmp_path):
    from backend.skills.registry import build_skill_registry
    _write_skill(tmp_path / "doc-coauthoring", "doc-coauthoring",
                 {"name": "doc-coauthoring", "title": "Doc Coauthoring", "version": "1.2.3"})
    reg = build_skill_registry(tmp_path)
    assert "doc-coauthoring" in reg
    s = reg.get("doc-coauthoring")
    assert s.title == "Doc Coauthoring"
    assert s.version == "1.2.3"
    assert s.body.strip() == "system body"


def test_skips_folders_without_skill_md(tmp_path):
    from backend.skills.registry import build_skill_registry
    (tmp_path / "empty").mkdir()
    (tmp_path / "_shared").mkdir()           # underscore prefix = always skipped
    (tmp_path / ".hidden").mkdir()           # dot prefix = always skipped
    _write_skill(tmp_path / "valid", "valid", {"name": "valid", "title": "Valid"})
    reg = build_skill_registry(tmp_path)
    assert list(reg.skills.keys()) == ["valid"]


def test_real_skills_directory_loads():
    """Ship-time check: every committed skill in skills/ loads cleanly."""
    from backend.skills.registry import build_skill_registry
    reg = build_skill_registry(ROOT / "skills")
    # We commit at least the 6 mirrored from Claude.
    expected = {
        "algorithmic-art", "canvas-design", "doc-coauthoring",
        "mcp-builder", "skill-creator", "web-artifacts-builder",
    }
    assert expected.issubset(set(reg.skills)), f"missing: {expected - set(reg.skills)}"
    for slug in expected:
        sk = reg.get(slug)
        assert sk.title
        assert sk.description
        assert sk.body.strip(), f"{slug} has empty body"


# ── render_combined_prompt + triggers ───────────────────────────────────────

def test_render_combined_prompt_concatenates(tmp_path):
    from backend.skills.registry import build_skill_registry
    _write_skill(tmp_path / "a", "a", {"name": "a", "title": "A"}, body="alpha body")
    _write_skill(tmp_path / "b", "b", {"name": "b", "title": "B"}, body="beta body")
    reg = build_skill_registry(tmp_path)
    out = reg.render_combined_prompt(["a", "b"])
    assert "alpha body" in out
    assert "beta body" in out
    assert out.index("alpha body") < out.index("beta body")
    assert '<skill name="a"' in out
    assert '<skill name="b"' in out


def test_render_combined_prompt_dedupes_and_skips_unknown(tmp_path):
    from backend.skills.registry import build_skill_registry
    _write_skill(tmp_path / "a", "a", {"name": "a", "title": "A"}, body="alpha")
    reg = build_skill_registry(tmp_path)
    out = reg.render_combined_prompt(["a", "a", "ghost"])
    assert out.count("alpha") == 1


def test_render_combined_prompt_respects_disabled(tmp_path):
    from backend.skills.registry import build_skill_registry
    _write_skill(tmp_path / "a", "a",
                 {"name": "a", "title": "A", "enabled": "false"}, body="alpha")
    reg = build_skill_registry(tmp_path)
    out = reg.render_combined_prompt(["a"])
    assert out == ""


def test_match_triggers(tmp_path):
    from backend.skills.registry import build_skill_registry
    folder = _write_skill(
        tmp_path / "designer", "designer",
        {"name": "designer", "title": "Designer"},
    )
    # Manually rewrite to add triggers (frontmatter list)
    (folder / "SKILL.md").write_text(textwrap.dedent("""\
        ---
        name: designer
        title: Designer
        triggers:
          - design a poster
          - logo idea
        ---
        body
    """))
    reg = build_skill_registry(tmp_path)
    hits = reg.match_triggers("Can you help me design a poster for the launch?")
    assert [s.slug for s in hits] == ["designer"]
    assert reg.match_triggers("unrelated question") == []
