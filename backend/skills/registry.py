"""Skill discovery and lookup.

Each skill lives at `skills/<slug>/SKILL.md`. Format:

    ---
    name: skill-creator
    title: Skill Creator
    description: Help author new local-ai-stack skill packs.
    version: 1.0.0
    triggers:
      - 'create a skill'
      - 'new skill pack'
    suggested_tier: versatile
    ---

    # System instructions

    You are operating as the *Skill Creator*. When the user...

The frontmatter is YAML; the body (everything after the second `---`)
is the system-prompt fragment that gets injected when the skill is
active. Template files (one or more) can sit alongside SKILL.md and
are exposed via :py:meth:`Skill.template_path` so the skill body can
reference them without hardcoding paths.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


logger = logging.getLogger(__name__)


_FM_DELIM = "---"


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return (parsed_frontmatter, body). Tolerant of missing frontmatter:
    a SKILL.md without `---` returns ({}, full_text)."""
    stripped = text.lstrip()
    if not stripped.startswith(_FM_DELIM):
        return {}, text
    # Skip leading whitespace before the opening delimiter, then find the
    # closing delimiter on its own line.
    after_open = stripped[len(_FM_DELIM):].lstrip("\n")
    closing = after_open.find("\n" + _FM_DELIM)
    if closing < 0:
        return {}, text
    fm_text = after_open[:closing]
    body = after_open[closing + len(_FM_DELIM) + 1:].lstrip("\n")
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        logger.warning("Bad YAML frontmatter: %s", e)
        return {}, text
    if not isinstance(fm, dict):
        return {}, text
    return fm, body


@dataclass
class Skill:
    slug: str                      # folder name; used as the `/skill <slug>` token
    title: str
    description: str
    body: str                      # raw system-prompt body
    version: str = "1.0.0"
    triggers: list[str] = field(default_factory=list)
    suggested_tier: str | None = None
    path: Path | None = None       # folder containing SKILL.md
    enabled: bool = True

    def template_path(self, name: str) -> Path | None:
        """Resolve a template/asset filename relative to the skill folder.
        Returns None if the file doesn't exist."""
        if not self.path:
            return None
        candidate = (self.path / "templates" / name).resolve()
        if candidate.exists():
            return candidate
        # Fall back to the skill folder root.
        candidate = (self.path / name).resolve()
        if candidate.exists():
            return candidate
        return None

    def render_system_prompt(self) -> str:
        """The body, prefixed with a ``<skill name="...">`` envelope so the
        model can recognise that an extra capability is in scope without
        leaking the slug into prose."""
        return (
            f"<skill name=\"{self.slug}\" title=\"{self.title}\">\n"
            f"{self.body.rstrip()}\n"
            f"</skill>"
        )


class SkillRegistry:
    def __init__(self) -> None:
        self.skills: dict[str, Skill] = {}

    def __contains__(self, slug: str) -> bool:
        return slug in self.skills

    def __len__(self) -> int:
        return len(self.skills)

    def get(self, slug: str) -> Skill | None:
        return self.skills.get(slug)

    def all(self) -> list[Skill]:
        return sorted(self.skills.values(), key=lambda s: s.title.lower())

    def enabled(self) -> list[Skill]:
        return [s for s in self.all() if s.enabled]

    def render_combined_prompt(self, slugs: list[str] | set[str]) -> str:
        """Concatenate the system-prompt fragments for an ordered list of
        active skills. Unknown / disabled slugs are silently skipped so a
        stale pin in a saved chat doesn't break the request."""
        parts: list[str] = []
        seen: set[str] = set()
        for slug in slugs:
            if slug in seen:
                continue
            seen.add(slug)
            sk = self.skills.get(slug)
            if not sk or not sk.enabled:
                continue
            parts.append(sk.render_system_prompt())
        return "\n\n".join(parts)

    def match_triggers(self, text: str) -> list[Skill]:
        """Find skills whose declared trigger phrases are substrings of the
        user message (case-insensitive). Used for soft auto-suggest in the
        chat UI; never auto-activates a skill server-side."""
        if not text:
            return []
        lowered = text.lower()
        out: list[Skill] = []
        for sk in self.all():
            if not sk.enabled:
                continue
            for trig in sk.triggers:
                if trig and trig.lower() in lowered:
                    out.append(sk)
                    break
        return out


def _load_skill(folder: Path) -> Skill | None:
    skill_md = folder / "SKILL.md"
    if not skill_md.exists():
        logger.debug("No SKILL.md in %s — skipping", folder.name)
        return None
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Failed to read %s: %s", skill_md, e)
        return None
    fm, body = _split_frontmatter(text)
    slug = str(fm.get("name") or folder.name).strip()
    title = str(fm.get("title") or slug.replace("-", " ").title()).strip()
    description = str(fm.get("description") or "").strip()
    version = str(fm.get("version") or "1.0.0").strip()
    triggers_raw = fm.get("triggers") or []
    triggers = [str(t).strip() for t in triggers_raw if str(t).strip()]
    suggested_tier = fm.get("suggested_tier")
    suggested_tier = str(suggested_tier).strip() if suggested_tier else None
    enabled = bool(fm.get("enabled", True))
    return Skill(
        slug=slug,
        title=title,
        description=description,
        body=body,
        version=version,
        triggers=triggers,
        suggested_tier=suggested_tier,
        path=folder,
        enabled=enabled,
    )


def build_skill_registry(skills_dir: Path) -> SkillRegistry:
    """Discover every immediate subfolder of ``skills_dir`` that contains
    a SKILL.md and load it. Folders without SKILL.md are silently ignored
    so the directory can also hold shared assets (e.g. a `_shared/`
    folder of icons referenced across packs)."""
    reg = SkillRegistry()
    if not skills_dir.exists():
        logger.info("Skills dir not found: %s — registry will be empty", skills_dir)
        return reg
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        sk = _load_skill(child)
        if sk:
            reg.skills[sk.slug] = sk
    logger.info("Loaded %d skills from %s", len(reg.skills), skills_dir)
    return reg
