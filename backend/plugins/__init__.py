"""Plugins subsystem — manifest-driven bundles of tools + skills.

Mirrors the Plugins panel Anthropic ships in Claude (Productivity,
Design, Data, Finance, Engineering, ...): a *plugin* is a thin manifest
that names which tools it activates and which skills it surfaces. There
is no extra runtime — toggling a plugin is equivalent to toggling
every member individually, just with a single switch.
"""

from .registry import (
    Plugin,
    PluginRegistry,
    build_plugin_registry,
)

__all__ = ["Plugin", "PluginRegistry", "build_plugin_registry"]
