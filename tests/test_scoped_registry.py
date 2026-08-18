"""Integration tests for scoped lifecycle wrappers around registries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, List
from unittest.mock import MagicMock, patch

import pytest

from leapflow.domain.effect_scope import EffectScope
from leapflow.domain.plugin_fiber import FiberState, PluginFiber
from leapflow.tools.protocol import ToolMetadata, ToolPlugin
from leapflow.tools.plugin_registry import ToolPluginRegistry
from leapflow.tools.scoped_registry import ScopedToolRegistry
from leapflow.gateway.scoped_adapter_registry import ScopedGatewayAdapterRegistry
from leapflow.llm.scoped_provider_registry import ScopedLLMProviderRegistry


# ════════════════════════════════════════════════════════════════
# Fake implementations for testing
# ════════════════════════════════════════════════════════════════


def _noop_handler(**kwargs: Any) -> str:
    return "ok"


def _make_tool_metadata(name: str) -> ToolMetadata:
    """Create a minimal ToolMetadata for testing."""
    return ToolMetadata(
        name=name,
        description=f"Test tool: {name}",
        parameters_schema={"type": "object", "properties": {}},
        handler=_noop_handler,
    )


@dataclass
class FakeToolPlugin:
    """Minimal ToolPlugin implementation for testing."""

    _plugin_id: str
    _tools: list[ToolMetadata] = field(default_factory=list)
    _category: str = "test"

    @property
    def plugin_id(self) -> str:
        return self._plugin_id

    @property
    def category(self) -> str:
        return self._category

    @property
    def tools(self) -> list[ToolMetadata]:
        return self._tools

    @property
    def dependencies(self) -> list[str]:
        return []

    def bind_runtime(self, **deps: Any) -> None:
        pass


class FakeGatewayAdapter:
    """Minimal gateway adapter for testing."""

    def __init__(self, platform_id: str) -> None:
        self.platform_id = platform_id


class FakeGatewayRegistry:
    """Minimal gateway adapter registry for testing."""

    def __init__(self) -> None:
        self._adapters: dict[str, Any] = {}

    def register(self, plugin: Any) -> None:
        self._adapters[plugin.platform_id] = plugin

    def unregister(self, platform_id: str) -> None:
        self._adapters.pop(platform_id, None)


class FakeLLMProvider:
    """Minimal LLM provider for testing."""

    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id


class FakeLLMRegistry:
    """Minimal LLM provider registry for testing."""

    def __init__(self) -> None:
        self._providers: dict[str, Any] = {}

    def register(self, plugin: Any) -> None:
        self._providers[plugin.provider_id] = plugin

    def unregister(self, provider_id: str) -> None:
        self._providers.pop(provider_id, None)


# ════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════


@pytest.fixture
def fresh_tool_registry() -> ToolPluginRegistry:
    """Create a fresh ToolPluginRegistry without any built-in plugins."""
    return ToolPluginRegistry()


@pytest.fixture
def gateway_registry() -> FakeGatewayRegistry:
    return FakeGatewayRegistry()


@pytest.fixture
def llm_registry() -> FakeLLMRegistry:
    return FakeLLMRegistry()


# ════════════════════════════════════════════════════════════════
# ScopedToolRegistry tests
# ════════════════════════════════════════════════════════════════


class TestScopedToolRegistryFullLifecycle:
    """Full lifecycle: create fiber → register → assemble → dispose → tools gone."""

    def test_scoped_tool_registry_full_lifecycle(self, fresh_tool_registry: ToolPluginRegistry) -> None:
        plugin = FakeToolPlugin(
            _plugin_id="test-plugin",
            _tools=[_make_tool_metadata("test_tool_alpha"), _make_tool_metadata("test_tool_beta")],
        )

        scoped = ScopedToolRegistry(fresh_tool_registry)
        fiber = scoped.create_fiber("test-plugin")
        scoped.scoped_register(plugin, fiber)

        # Assemble to populate handler/definition structures
        # Patch bridge adapter to avoid importing real bridge dependencies
        with patch.object(fresh_tool_registry, "_get_bridge_adapter") as mock_bridge:
            mock_bridge.return_value = MagicMock()
            fresh_tool_registry.assemble()

        # Verify tools are present after assembly
        assert "test_tool_alpha" in fresh_tool_registry._tool_handlers
        assert "test_tool_beta" in fresh_tool_registry._tool_handlers
        assert "test-plugin" in fresh_tool_registry._plugins

        # Dispose
        fiber.activate()
        fiber.begin_unload()
        fiber.dispose()

        # Verify tools are removed
        assert "test_tool_alpha" not in fresh_tool_registry._tool_handlers
        assert "test_tool_beta" not in fresh_tool_registry._tool_handlers
        assert "test-plugin" not in fresh_tool_registry._plugins


class TestScopedToolRegistryGpAliases:
    """gp_ prefixed aliases are also cleaned up."""

    def test_scoped_tool_registry_removes_gp_aliases(self, fresh_tool_registry: ToolPluginRegistry) -> None:
        plugin = FakeToolPlugin(
            _plugin_id="alias-plugin",
            _tools=[_make_tool_metadata("my_tool")],
        )

        scoped = ScopedToolRegistry(fresh_tool_registry)
        fiber = scoped.create_fiber("alias-plugin")
        scoped.scoped_register(plugin, fiber)

        with patch.object(fresh_tool_registry, "_get_bridge_adapter") as mock_bridge:
            mock_bridge.return_value = MagicMock()
            fresh_tool_registry.assemble()

        # Simulate gp_ alias being present (as bridge adapter would create)
        fresh_tool_registry._tool_handlers["gp_my_tool"] = _noop_handler

        # Dispose
        fiber.activate()
        fiber.begin_unload()
        fiber.dispose()

        assert "my_tool" not in fresh_tool_registry._tool_handlers
        assert "gp_my_tool" not in fresh_tool_registry._tool_handlers


class TestScopedToolRegistryAllStructures:
    """Verify _plugins, _tool_handlers, _tool_definitions, _all_metadata all cleaned."""

    def test_scoped_tool_registry_cleanup_removes_from_all_structures(
        self, fresh_tool_registry: ToolPluginRegistry
    ) -> None:
        plugin = FakeToolPlugin(
            _plugin_id="full-clean",
            _tools=[_make_tool_metadata("clean_tool")],
        )

        scoped = ScopedToolRegistry(fresh_tool_registry)
        fiber = scoped.create_fiber("full-clean")
        scoped.scoped_register(plugin, fiber)

        with patch.object(fresh_tool_registry, "_get_bridge_adapter") as mock_bridge:
            mock_bridge.return_value = MagicMock()
            fresh_tool_registry.assemble()

        # Pre-conditions: everything present
        assert "full-clean" in fresh_tool_registry._plugins
        assert "clean_tool" in fresh_tool_registry._tool_handlers
        assert any(
            d.get("function", {}).get("name") == "clean_tool"
            for d in fresh_tool_registry._tool_definitions
        )
        assert any(m.name == "clean_tool" for m in fresh_tool_registry._all_metadata)

        # Dispose
        fiber.activate()
        fiber.begin_unload()
        fiber.dispose()

        # Post-conditions: everything gone
        assert "full-clean" not in fresh_tool_registry._plugins
        assert "clean_tool" not in fresh_tool_registry._tool_handlers
        assert not any(
            d.get("function", {}).get("name") == "clean_tool"
            for d in fresh_tool_registry._tool_definitions
        )
        assert not any(m.name == "clean_tool" for m in fresh_tool_registry._all_metadata)


class TestScopedToolRegistryMultiplePlugins:
    """Disposing one plugin doesn't affect another."""

    def test_scoped_tool_registry_multiple_plugins_isolated(
        self, fresh_tool_registry: ToolPluginRegistry
    ) -> None:
        plugin_a = FakeToolPlugin(
            _plugin_id="plugin-a",
            _tools=[_make_tool_metadata("tool_a")],
        )
        plugin_b = FakeToolPlugin(
            _plugin_id="plugin-b",
            _tools=[_make_tool_metadata("tool_b")],
        )

        scoped = ScopedToolRegistry(fresh_tool_registry)
        fiber_a = scoped.create_fiber("plugin-a")
        fiber_b = scoped.create_fiber("plugin-b")
        scoped.scoped_register(plugin_a, fiber_a)
        scoped.scoped_register(plugin_b, fiber_b)

        with patch.object(fresh_tool_registry, "_get_bridge_adapter") as mock_bridge:
            mock_bridge.return_value = MagicMock()
            fresh_tool_registry.assemble()

        # Dispose only plugin-a
        fiber_a.activate()
        fiber_a.begin_unload()
        fiber_a.dispose()

        assert "tool_a" not in fresh_tool_registry._tool_handlers
        assert "plugin-a" not in fresh_tool_registry._plugins
        # plugin-b is untouched
        assert "tool_b" in fresh_tool_registry._tool_handlers
        assert "plugin-b" in fresh_tool_registry._plugins


class TestScopedToolRegistryLateTool:
    """Late-registered tool is cleaned up on dispose."""

    def test_scoped_tool_registry_late_tool_lifecycle(
        self, fresh_tool_registry: ToolPluginRegistry
    ) -> None:
        # First assemble with an empty plugin set (or just leave assembled=False)
        with patch.object(fresh_tool_registry, "_get_bridge_adapter") as mock_bridge:
            mock_bridge.return_value = MagicMock()
            fresh_tool_registry.assemble()

        scoped = ScopedToolRegistry(fresh_tool_registry)
        fiber = scoped.create_fiber("late-plugin")
        fiber.activate()

        # Register a late tool
        late_def = {"type": "function", "function": {"name": "late_tool", "parameters": {}}}

        # Patch register_late_tool to avoid bridge adapter issues
        with patch.object(fresh_tool_registry, "_get_bridge_adapter") as mock_bridge:
            mock_bridge.return_value = MagicMock()
            scoped.scoped_register_late_tool(late_def, _noop_handler, "late_tool", fiber)

        assert "late_tool" in fresh_tool_registry._tool_handlers

        # Dispose
        fiber.begin_unload()
        fiber.dispose()

        assert "late_tool" not in fresh_tool_registry._tool_handlers


# ════════════════════════════════════════════════════════════════
# ScopedGatewayAdapterRegistry tests
# ════════════════════════════════════════════════════════════════


class TestScopedGatewayAdapterRegistry:
    """Gateway adapter lifecycle via scoped wrapper."""

    def test_scoped_gateway_register_and_dispose(self, gateway_registry: FakeGatewayRegistry) -> None:
        adapter = FakeGatewayAdapter("feishu")
        scoped = ScopedGatewayAdapterRegistry(gateway_registry)
        fiber = scoped.create_fiber("feishu")
        scoped.scoped_register(adapter, fiber)

        # Verify registered
        assert "feishu" in gateway_registry._adapters

        # Dispose
        fiber.activate()
        fiber.begin_unload()
        fiber.dispose()

        # Verify unregistered
        assert "feishu" not in gateway_registry._adapters

    def test_scoped_gateway_uses_existing_unregister(self, gateway_registry: FakeGatewayRegistry) -> None:
        """Verify it delegates to the underlying registry's unregister() method."""
        adapter = FakeGatewayAdapter("slack")
        scoped = ScopedGatewayAdapterRegistry(gateway_registry)
        fiber = scoped.create_fiber("slack")

        # Patch unregister to verify it's called
        original_unregister = gateway_registry.unregister
        unregister_calls: list[str] = []

        def tracking_unregister(platform_id: str) -> None:
            unregister_calls.append(platform_id)
            original_unregister(platform_id)

        gateway_registry.unregister = tracking_unregister

        scoped.scoped_register(adapter, fiber)
        fiber.activate()
        fiber.begin_unload()
        fiber.dispose()

        assert "slack" in unregister_calls


# ════════════════════════════════════════════════════════════════
# ScopedLLMProviderRegistry tests
# ════════════════════════════════════════════════════════════════


class TestScopedLLMProviderRegistry:
    """LLM provider lifecycle via scoped wrapper."""

    def test_scoped_llm_register_and_dispose(self, llm_registry: FakeLLMRegistry) -> None:
        provider = FakeLLMProvider("openai-custom")
        scoped = ScopedLLMProviderRegistry(llm_registry)
        fiber = scoped.create_fiber("openai-custom")
        scoped.scoped_register(provider, fiber)

        # Verify registered
        assert "openai-custom" in llm_registry._providers

        # Dispose
        fiber.activate()
        fiber.begin_unload()
        fiber.dispose()

        # Verify unregistered
        assert "openai-custom" not in llm_registry._providers
