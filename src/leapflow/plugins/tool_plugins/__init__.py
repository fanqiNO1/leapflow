"""Built-in tool plugin discovery.

Each module in this package exposes a module-level ``plugin`` instance
satisfying the ToolPlugin Protocol. ``get_all_plugins()`` aggregates them for
``ToolPluginRegistry.discover_builtin()``.

To add a built-in plugin: create the module, expose ``plugin``, and list it in
``_BUILTIN_PLUGIN_MODULES`` below.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from leapflow.plugins.protocol import ToolPlugin

logger = logging.getLogger(__name__)

# Discovery order is a product contract: it fixes the order tools appear in the
# LLM tool index, which is part of the system prompt the journey cassettes are
# fingerprinted against. Do not reorder without reseeding cassettes.
_BUILTIN_PLUGIN_MODULES = (
    "leapflow.plugins.tool_plugins.text_utils",
    "leapflow.plugins.tool_plugins.system_info",
    "leapflow.plugins.tool_plugins.skill_discovery",
    "leapflow.plugins.tool_plugins.code_intel",
    "leapflow.plugins.tool_plugins.scm_git",
    "leapflow.plugins.tool_plugins.dev_tools",
    "leapflow.plugins.tool_plugins.file_ops",
    "leapflow.plugins.tool_plugins.shell_terminal",
    "leapflow.plugins.tool_plugins.config_tools",
    "leapflow.plugins.tool_plugins.web_access",
    "leapflow.plugins.tool_plugins.memory_research",
    "leapflow.plugins.tool_plugins.orchestration",
    "leapflow.plugins.tool_plugins.hub",
    "leapflow.plugins.tool_plugins.gateway",
    "leapflow.plugins.tool_plugins.self_management",
    # Desktop semantics — tools activate only once perception is bound.
    "leapflow.plugins.tool_plugins.desktop_semantic",
)


def _disabled_plugin_ids() -> set[str]:
    """Read ``disabled_plugins`` from settings, tolerating early bootstrap."""
    try:
        from leapflow.config import get_settings

        return set(getattr(get_settings(), "disabled_plugins", ()) or ())
    except (ImportError, AttributeError, RuntimeError):
        # Config not available during early init; treat as no filter.
        return set()


def _discover_all() -> "list[ToolPlugin]":
    """Import all built-in plugin modules and collect their plugin instances."""
    disabled = _disabled_plugin_ids()
    plugins: list[ToolPlugin] = []

    for module_path in _BUILTIN_PLUGIN_MODULES:
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            logger.error("Failed to import plugin module %s: %s", module_path, exc)
            continue

        plugin = getattr(module, "plugin", None)
        if plugin is None:
            logger.warning(
                "Plugin module %s does not define a 'plugin' variable", module_path
            )
            continue
        if plugin.plugin_id in disabled:
            logger.info("Skipping disabled plugin: %s", plugin.plugin_id)
            continue
        plugins.append(plugin)

    return plugins


# Lazy singleton — no side effects at import time.
_all_plugins: "list[ToolPlugin] | None" = None


def get_all_plugins() -> "list[ToolPlugin]":
    """Return all built-in plugin instances, discovering lazily on first access."""
    global _all_plugins
    if _all_plugins is None:
        _all_plugins = _discover_all()
    return _all_plugins


__all__ = ["get_all_plugins"]
