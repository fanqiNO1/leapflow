"""Tool system public API — single entry point for all consumers.

The ToolPluginRegistry is the authoritative source for tool definitions,
handlers, and runtime gates. All consumers import from this module.
"""

from __future__ import annotations

from typing import Any, Optional, TYPE_CHECKING

from leapflow.tools.protocol import ToolMetadata, ToolPlugin
from leapflow.tools.plugin_registry import ToolPluginRegistry
from leapflow.tools.bridge_adapter import ToolBridgeAdapter

if TYPE_CHECKING:
    from leapflow.tools.scoped_registry import ScopedToolRegistry

# Lazy singleton — no side effects at import time.
_registry: ToolPluginRegistry | None = None
_scoped_registry: Optional["ScopedToolRegistry"] = None


def get_registry() -> ToolPluginRegistry:
    """Return the process-global ToolPluginRegistry, initializing lazily on first access."""
    global _registry
    if _registry is None:
        reg = ToolPluginRegistry()
        reg.discover_builtin()
        _registry = reg
    return _registry


def get_scoped_registry() -> "ScopedToolRegistry":
    """Return a ScopedToolRegistry wrapping the global registry.

    On first creation, every plugin already registered on the underlying
    registry (all built-ins discovered at boot) is adopted under a
    PluginFiber so the whole tool subsystem is uniformly under fiber
    lifecycle management. Adoption is additive tracking only — it does not
    re-register plugins or change how tools are dispatched.
    """
    global _scoped_registry
    if _scoped_registry is None:
        from leapflow.tools.scoped_registry import ScopedToolRegistry
        _scoped_registry = ScopedToolRegistry(get_registry())
        _scoped_registry.adopt_existing_plugins()
    return _scoped_registry


def get_bridge_adapter() -> ToolBridgeAdapter:
    """Return the ToolBridgeAdapter for the process-global registry."""
    return get_registry()._get_bridge_adapter()


def reload_plugin(plugin_id: str):
    """Reload a plugin at runtime.

    Convenience wrapper around get_scoped_registry().reload(plugin_id).
    Returns the new PluginFiber in ACTIVE state.
    """
    return get_scoped_registry().reload(plugin_id)


def bootstrap_tools(bridge: "Any") -> int:  # noqa: F821
    """Legacy ToolBridge compat layer — remove when ToolBridge is eliminated.

    Registers all GP tools into a ToolBridge instance with 'gp_' prefix so
    the engine's _execute_general_tool can dispatch through ToolBridge. The
    engine still uses ToolBridge as its primary dispatch path (before falling
    back to registry.tool_handlers).

    Returns:
        Number of tools successfully registered.
    """
    return get_bridge_adapter().bootstrap(bridge)


# Module-level __getattr__ for backward compat: `from leapflow.tools import registry`
def __getattr__(name: str) -> Any:
    if name == "registry":
        return get_registry()
    raise AttributeError(f"module 'leapflow.tools' has no attribute {name!r}")


__all__ = [
    "registry",
    "get_registry",
    "get_scoped_registry",
    "get_bridge_adapter",
    "reload_plugin",
    "bootstrap_tools",
    "ToolBridgeAdapter",
    "ToolMetadata",
    "ToolPlugin",
    "ToolPluginRegistry",
]
