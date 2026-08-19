"""Scoped lifecycle wrapper for ToolPluginRegistry.

Provides reversible plugin registration: registering through this wrapper
automatically tracks cleanup effects on a PluginFiber's scope. When the
fiber is disposed, the plugin's tools are removed from the underlying registry.

This is a composition wrapper — the underlying ToolPluginRegistry is NOT modified.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from leapflow.domain.effect_scope import EffectScope
from leapflow.domain.plugin_fiber import PluginFiber, FiberState
from leapflow.plugins.protocol import ToolPlugin

logger = logging.getLogger(__name__)


class ScopedToolRegistry:
    """Composition wrapper adding lifecycle management to ToolPluginRegistry.

    Usage:
        from leapflow.plugins import get_registry
        registry = get_registry()
        scoped = ScopedToolRegistry(registry)

        fiber = scoped.create_fiber("my-plugin")
        scoped.scoped_register(my_plugin, fiber)
        fiber.activate()
        # ... plugin tools are now available ...
        fiber.begin_unload()
        fiber.dispose()  # tools automatically removed
    """

    def __init__(self, registry: Any) -> None:
        """Wrap an existing ToolPluginRegistry instance."""
        self._registry = registry
        self._fibers: dict[str, PluginFiber] = {}
        self._plugin_modules: dict[str, str] = {}  # plugin_id → module path

    def create_fiber(self, plugin_id: str) -> PluginFiber:
        """Create a new PluginFiber for managing a plugin's lifecycle."""
        scope = EffectScope(f"tool-plugin:{plugin_id}")
        fiber = PluginFiber(plugin_id=plugin_id, scope=scope)
        self._fibers[plugin_id] = fiber
        return fiber

    def get_fiber(self, plugin_id: str) -> Optional[PluginFiber]:
        """Get an existing fiber by plugin ID."""
        return self._fibers.get(plugin_id)

    def scoped_register(self, plugin: ToolPlugin, fiber: PluginFiber) -> None:
        """Register a plugin with lifecycle tracking.

        The plugin is registered on the underlying registry, and a cleanup
        effect is added to the fiber's scope that will remove all the plugin's
        tools when the fiber is disposed.
        """
        plugin_id = plugin.plugin_id
        # Track module path so reload() can re-import the plugin later.
        self._plugin_modules[plugin_id] = plugin.__class__.__module__
        # Register on underlying registry
        self._registry.register(plugin)

        # Capture tool names for cleanup
        tool_names = [t.name for t in plugin.tools]

        # Register cleanup effect on the fiber's scope
        def _cleanup() -> None:
            self._unregister_tools(plugin_id, tool_names)

        fiber.scope.effect(_cleanup)
        logger.debug("Scoped-registered plugin '%s' with %d tools", plugin_id, len(tool_names))

    def scoped_register_late_tool(
        self,
        definition: dict[str, Any],
        handler: Any,
        name: str,
        fiber: PluginFiber,
    ) -> None:
        """Register a late tool with lifecycle tracking."""
        self._registry.register_late_tool(definition, handler, name)

        def _cleanup() -> None:
            self._remove_late_tool(name)

        fiber.scope.effect(_cleanup)

    def _unregister_tools(self, plugin_id: str, tool_names: list[str]) -> None:
        """Cleanup callback: remove plugin+tools from the underlying registry.

        Delegates to ToolPluginRegistry public API to preserve encapsulation.
        """
        # Try full plugin removal first (also removes from _plugins dict)
        if not self._registry.unregister_plugin(plugin_id):
            # Fallback: plugin not in registry (may have been removed already);
            # ensure tool names are cleaned up anyway.
            self._registry.unregister_tools(tool_names)

    def _remove_late_tool(self, name: str) -> None:
        """Remove a single late-registered tool via public API."""
        self._registry.unregister_tools([name])

    def adopt_existing_plugins(self) -> None:
        """Create fibers for plugins already registered directly on the underlying registry.

        Used during boot to bring all built-in plugins under fiber lifecycle management
        WITHOUT re-registering them (which would raise Duplicate plugin_id).
        """
        for plugin_id, plugin in self._registry.plugins.items():
            if plugin_id in self._fibers:
                continue  # already adopted
            fiber = self.create_fiber(plugin_id)
            self._plugin_modules[plugin_id] = plugin.__class__.__module__
            tool_names = [t.name for t in plugin.tools]

            def _cleanup(pid: str = plugin_id, names: list = tool_names) -> None:
                self._unregister_tools(pid, names)

            fiber.scope.effect(_cleanup)
            fiber.activate()

    @property
    def fibers(self) -> dict[str, PluginFiber]:
        """Read-only view of managed fibers."""
        return dict(self._fibers)

    def reload(self, plugin_id: str) -> PluginFiber:
        """Reload a plugin: dispose old fiber, re-import module, register new instance.

        Returns the new PluginFiber in ACTIVE state.

        Raises:
            KeyError: if plugin_id was never scoped-registered.
            RuntimeError: if the plugin module cannot be reloaded or has no `plugin` attribute.

        Concurrency safety:
            LeapFlow's engine snapshots handlers per-turn via `dict(_plugin_registry.tool_handlers)`.
            Existing turns keep their snapshot and finish with old handlers. New turns starting
            after this call pick up the new handlers. Single-threaded asyncio ensures no
            mid-turn tool table swap.

        Late-bound dependency re-injection:
            After the new fiber is activated, the registry's last_bound_deps are re-applied
            via bind_runtime(). This ensures gates, managers, and other runtime deps that
            were previously injected are also available to the new plugin instance.
        """
        if plugin_id not in self._fibers:
            raise KeyError(f"Plugin '{plugin_id}' not scoped-registered")

        module_path = self._plugin_modules.get(plugin_id)
        if module_path is None:
            raise RuntimeError(f"Module path unknown for plugin '{plugin_id}'")

        old_fiber = self._fibers[plugin_id]
        old_tool_names: list[str] = []
        # Capture current tool names BEFORE disposing so we know what to remove.
        old_plugin = self._registry.get_plugin(plugin_id)
        if old_plugin is not None:
            old_tool_names = [t.name for t in old_plugin.tools]

        # 1. Dispose old fiber (EffectScope cleanup runs unregister)
        if old_fiber.state == FiberState.ACTIVE:
            old_fiber.begin_unload()
        if old_fiber.state != FiberState.DISPOSED:
            old_fiber.dispose()

        # Belt-and-suspenders: fiber.dispose() already triggered scope cleanup which
        # should have called unregister_plugin. This is defensive in case the effect
        # callback didn't run (e.g., disposed via a different path). It's idempotent.
        if old_tool_names:
            self._unregister_tools(plugin_id, old_tool_names)

        # 2. Re-import the plugin module to get a fresh instance
        import importlib
        import sys
        if module_path not in sys.modules:
            raise RuntimeError(
                f"Plugin module '{module_path}' not in sys.modules; cannot reload"
            )
        fresh_module = importlib.reload(sys.modules[module_path])
        fresh_plugin = getattr(fresh_module, "plugin", None)
        if fresh_plugin is None:
            raise RuntimeError(
                f"Reloaded module '{module_path}' has no 'plugin' attribute"
            )

        # 3. Create new fiber and register the fresh plugin
        new_fiber = self.create_fiber(plugin_id)
        self.scoped_register(fresh_plugin, new_fiber)
        new_fiber.activate()

        # 4. Publish the fresh plugin's tools into the already-assembled catalog
        #    and bump the registry version so consumer caches (e.g. the engine
        #    tool registry) rebuild on the next turn.
        self._registry.publish_plugin_tools(fresh_plugin)

        # 5. Re-inject last-bound runtime dependencies onto the new plugin instance
        if self._registry.last_bound_deps:
            self._registry.bind_runtime(**self._registry.last_bound_deps)

        return new_fiber
