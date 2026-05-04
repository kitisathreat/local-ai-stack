"""Integration test: ChatRequest.enabled_skills + /skill slash command both
land in the system prompt."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _write_skill(folder: Path, slug: str, body: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {slug}\ntitle: {slug.title()}\n---\n{body}\n"
    )


def test_inject_skills_prepends_to_system_message(tmp_path):
    from backend.middleware.context import inject_skills
    from backend.schemas import ChatMessage
    from backend.skills.registry import build_skill_registry

    _write_skill(tmp_path / "alpha", "alpha", "ALPHA SKILL BODY")
    reg = build_skill_registry(tmp_path)

    msgs = [
        ChatMessage(role="system", content="existing system prompt"),
        ChatMessage(role="user", content="hello"),
    ]
    inject_skills(msgs, reg, ["alpha"])
    assert msgs[0].role == "system"
    assert "ALPHA SKILL BODY" in msgs[0].content
    assert "existing system prompt" in msgs[0].content
    # Skills must come BEFORE the existing prompt
    assert msgs[0].content.index("ALPHA SKILL BODY") < msgs[0].content.index("existing system prompt")


def test_inject_skills_inserts_when_no_system_msg(tmp_path):
    from backend.middleware.context import inject_skills
    from backend.schemas import ChatMessage
    from backend.skills.registry import build_skill_registry

    _write_skill(tmp_path / "beta", "beta", "BETA SKILL")
    reg = build_skill_registry(tmp_path)

    msgs = [ChatMessage(role="user", content="hi")]
    inject_skills(msgs, reg, ["beta"])
    assert msgs[0].role == "system"
    assert "BETA SKILL" in msgs[0].content


def test_inject_skills_noop_when_no_slugs(tmp_path):
    from backend.middleware.context import inject_skills
    from backend.schemas import ChatMessage
    from backend.skills.registry import SkillRegistry

    msgs = [ChatMessage(role="user", content="hi")]
    inject_skills(msgs, SkillRegistry(), None)
    assert len(msgs) == 1
    inject_skills(msgs, SkillRegistry(), [])
    assert len(msgs) == 1


def test_slash_skill_parsed_into_skills_list():
    """/skill <slug> should populate SlashParseResult.skills and chain."""
    from backend.router import parse_slash_commands

    slash_map = {"/skill": {"set_skill": True}, "/think on": {"think": True}}
    result = parse_slash_commands(
        "/skill doc-coauthoring /skill mcp-builder /think on write me a tool",
        slash_map,
    )
    assert "doc-coauthoring" in result.skills
    assert "mcp-builder" in result.skills
    assert result.think_override is True
    assert result.cleaned_message == "write me a tool"
