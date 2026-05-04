"""Skills subsystem — load `skills/<name>/SKILL.md` packs and surface
them as system-prompt prefixes the user can opt into per-chat.

Mirrors the model Anthropic ships in Claude (Personal skills): each
skill is a self-contained folder with a SKILL.md (frontmatter +
instructions) and optional templates/. Skills are stateless prompt
extensions — they don't execute code by themselves. When a user
invokes `/skill <name>` (or toggles one on in the chat UI), the
skill's instructions are prepended to the system prompt for the
duration of the request.
"""

from .registry import (
    Skill,
    SkillRegistry,
    build_skill_registry,
)

__all__ = ["Skill", "SkillRegistry", "build_skill_registry"]
