"""Built-in tool plugins discovery module.

Each plugin module in this package exposes a module-level `plugin` instance
that satisfies the ToolPlugin Protocol. get_all_plugins() aggregates them
for the registry's discover_builtin() method.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from leapflow.tools.protocol import ToolPlugin


def _discover_all() -> "list[ToolPlugin]":
    """Import all built-in plugin modules and collect their plugin instances."""
    from leapflow.tools.protocol import ToolPlugin as _TP  # noqa: F811

    plugins: list[_TP] = []

    # Import is deferred to avoid import-time side effects.
    # Each module listed here must define: `plugin = XxxPlugin()`
    _plugin_modules = [
        # Phase 2 pilot:
        "leapflow.tools.plugins.text_utils",
        # Phase 3 (low risk):
        "leapflow.tools.plugins.system_info",
        "leapflow.tools.plugins.skill_discovery",
        "leapflow.tools.plugins.code_intel",
        "leapflow.tools.plugins.scm_git",
        "leapflow.tools.plugins.dev_tools",
        # Phase 4 (medium risk):
        "leapflow.tools.plugins.file_ops",
        "leapflow.tools.plugins.shell_terminal",
        "leapflow.tools.plugins.config_tools",
        "leapflow.tools.plugins.web_access",
        # Phase 5 (high complexity):
        "leapflow.tools.plugins.memory_research",
        "leapflow.tools.plugins.orchestration",
        # External integrations:
        "leapflow.tools.plugins.hub",
        "leapflow.tools.plugins.gateway",
        # Phase 2.4 Self-Modification MVP:
        "leapflow.tools.plugins.self_management",
    ]

    # Read disabled_plugins from settings (safe during early bootstrap)
    disabled: set[str] = set()
    try:
        from leapflow.config import get_settings
        settings = get_settings()
        disabled = set(getattr(settings, "disabled_plugins", ()) or ())
    except (ImportError, AttributeError, RuntimeError):
        # Config not available during early init; treat as no filter.
        pass

    import importlib
    import logging

    for module_path in _plugin_modules:
        try:
            mod = importlib.import_module(module_path)
            plugin_instance = getattr(mod, "plugin", None)
            if plugin_instance is not None:
                if plugin_instance.plugin_id in disabled:
                    logging.getLogger(__name__).info(
                        "Skipping disabled plugin: %s", plugin_instance.plugin_id
                    )
                    continue
                plugins.append(plugin_instance)
            else:
                logging.getLogger(__name__).warning(
                    "Plugin module %s does not define a 'plugin' variable", module_path
                )
        except ImportError as e:
            logging.getLogger(__name__).error(
                "Failed to import plugin module %s: %s", module_path, e
            )

    return plugins


# Lazy singleton — no side effects at import time.
_all_plugins: "list[ToolPlugin] | None" = None


def get_all_plugins() -> "list[ToolPlugin]":
    """Return all built-in plugin instances, discovering lazily on first access."""
    global _all_plugins
    if _all_plugins is None:
        _all_plugins = _discover_all()
    return _all_plugins


# Module-level __getattr__ for backward compat: `from leapflow.tools.plugins import ALL_PLUGINS`
def __getattr__(name: str) -> Any:
    if name == "ALL_PLUGINS":
        return get_all_plugins()
    raise AttributeError(f"module 'leapflow.tools.plugins' has no attribute {name!r}")


def discover_plugin(module_path: str):
    """Import (or re-import) a specific plugin module and return its `plugin` instance.

    Used by reload machinery to fetch a fresh plugin instance without touching
    the ALL_PLUGINS singleton.
    """
    import importlib
    mod = importlib.import_module(module_path)
    plugin_instance = getattr(mod, "plugin", None)
    if plugin_instance is None:
        raise RuntimeError(f"Module '{module_path}' has no 'plugin' attribute")
    return plugin_instance
