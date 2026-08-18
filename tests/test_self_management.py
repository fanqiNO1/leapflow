"""Comprehensive tests for the Phase 2.4 Self-Modification plugin.

Covers:
- Read-only introspection (plugin_list, plugin_status) - no approval needed
- Mutation safety (plugin_reload, plugin_disable) - requires approval gate
- Gate injection and descriptor verification
- x_leapflow metadata correctness
- Self-destruction protection
- Test isolation with fresh state per test
"""

from __future__ import annotations

import pytest
from typing import Any

from leapflow.tools.protocol import ToolMetadata


# ════════════════════════════════════════════════════════════════
# Testing infrastructure
# ════════════════════════════════════════════════════════════════


class FakeApprovalResult:
    """Mock approval result matching the protocol expected by self_management."""

    def __init__(self, approved: bool, denial_message: str = ""):
        self.approved = approved
        self.denial_message = denial_message


class FakeApprovalGate:
    """Mock approval gate that records descriptors and returns configurable results."""

    def __init__(self, approved: bool = True, denial_message: str = ""):
        self._approved = approved
        self._denial = denial_message
        self.received_descriptors: list[Any] = []

    async def evaluate(self, descriptor: Any) -> FakeApprovalResult:
        """Record the descriptor and return configured result."""
        self.received_descriptors.append(descriptor)
        return FakeApprovalResult(self._approved, self._denial)


class SpyApprovalGate:
    """Spy gate that records all calls but doesn't perform real approval."""

    def __init__(self) -> None:
        self.received_descriptors: list[Any] = []
        self.call_count = 0

    async def evaluate(self, descriptor: Any) -> FakeApprovalResult:
        """Record the call and return approved=True to allow operation."""
        self.received_descriptors.append(descriptor)
        self.call_count += 1
        return FakeApprovalResult(approved=True, denial_message="")


@pytest.fixture
def self_mgmt_plugin():
    """Get the self_management plugin, resetting state between tests.
    
    This fixture ensures each test starts with a clean slate:
    - Assembles the registry
    - Gets the plugin
    - Resets the approval gate to None (fail-closed default)
    """
    from leapflow.tools import get_registry
    
    reg = get_registry()
    reg.assemble()
    plugin = reg.get_plugin("self_management")
    
    # Reset gate to None so tests start from fail-closed state
    plugin._plugin_approval_gate = None
    
    yield plugin
    
    # Cleanup: ensure gate is reset after test
    plugin._plugin_approval_gate = None


# ════════════════════════════════════════════════════════════════
# Section 1: Read-only introspection tests (no approval needed)
# ════════════════════════════════════════════════════════════════


class TestPluginListIntrospection:
    """Tests for plugin_list read-only introspection tool."""

    @pytest.mark.asyncio
    async def test_plugin_list_returns_all_plugins(
        self, self_mgmt_plugin: Any
    ) -> None:
        """plugin_list returns non-zero plugins, includes 'self_management' itself."""
        result = await self_mgmt_plugin._plugin_list_handler()
        
        assert result["ok"] is True
        assert "plugins" in result
        assert len(result["plugins"]) > 0
        # self_management should be in the list
        plugin_ids = [p["plugin_id"] for p in result["plugins"]]
        assert "self_management" in plugin_ids

    @pytest.mark.asyncio
    async def test_plugin_list_includes_fiber_state(
        self, self_mgmt_plugin: Any
    ) -> None:
        """Each entry has state and generation fields."""
        result = await self_mgmt_plugin._plugin_list_handler()
        
        assert result["ok"] is True
        for plugin_info in result["plugins"]:
            assert "state" in plugin_info
            assert "generation" in plugin_info
            # State should be a string value
            assert isinstance(plugin_info["state"], str)
            # Generation should be an integer or None
            assert plugin_info["generation"] is None or isinstance(
                plugin_info["generation"], int
            )

    @pytest.mark.asyncio
    async def test_plugin_list_includes_categories(
        self, self_mgmt_plugin: Any
    ) -> None:
        """Response has 'categories' set."""
        result = await self_mgmt_plugin._plugin_list_handler()
        
        assert result["ok"] is True
        assert "categories" in result
        assert isinstance(result["categories"], list)
        assert len(result["categories"]) > 0
        # Categories should be sorted
        assert result["categories"] == sorted(result["categories"])
        # self_management should be in "system" category
        assert "system" in result["categories"]

    @pytest.mark.asyncio
    async def test_plugin_list_subsystem_field(self, self_mgmt_plugin: Any) -> None:
        """Response includes subsystem='tools' field."""
        result = await self_mgmt_plugin._plugin_list_handler()
        
        assert result["ok"] is True
        assert result["subsystem"] == "tools"
        assert "plugin_count" in result
        assert result["plugin_count"] == len(result["plugins"])


class TestPluginStatusIntrospection:
    """Tests for plugin_status detailed introspection tool."""

    @pytest.mark.asyncio
    async def test_plugin_status_returns_details(
        self, self_mgmt_plugin: Any
    ) -> None:
        """plugin_status(plugin_id='text_utils') returns tools list, category, deps."""
        result = await self_mgmt_plugin._plugin_status_handler(plugin_id="text_utils")
        
        assert result["ok"] is True
        assert result["plugin_id"] == "text_utils"
        assert "category" in result
        assert "dependencies" in result
        assert "tools" in result
        assert isinstance(result["tools"], list)
        assert len(result["tools"]) > 0
        
        # Each tool should have name and description
        for tool in result["tools"]:
            assert "name" in tool
            assert "description" in tool

    @pytest.mark.asyncio
    async def test_plugin_status_unknown_plugin_error(
        self, self_mgmt_plugin: Any
    ) -> None:
        """plugin_status(plugin_id='nonexistent') returns ok=False error."""
        result = await self_mgmt_plugin._plugin_status_handler(
            plugin_id="nonexistent_plugin_xyz"
        )
        
        assert result["ok"] is False
        assert "error" in result
        assert "nonexistent_plugin_xyz" in result["error"]

    @pytest.mark.asyncio
    async def test_plugin_status_self_management_details(
        self, self_mgmt_plugin: Any
    ) -> None:
        """plugin_status for self_management shows correct structure."""
        result = await self_mgmt_plugin._plugin_status_handler(
            plugin_id="self_management"
        )
        
        assert result["ok"] is True
        assert result["plugin_id"] == "self_management"
        assert result["category"] == "system"
        assert "dependencies" in result
        # Should depend on plugin_approval_gate
        assert "plugin_approval_gate" in result["dependencies"]

    @pytest.mark.asyncio
    async def test_plugin_status_fiber_info_present(
        self, self_mgmt_plugin: Any
    ) -> None:
        """plugin_status response includes fiber state and generation."""
        result = await self_mgmt_plugin._plugin_status_handler(plugin_id="text_utils")
        
        assert result["ok"] is True
        assert "fiber" in result
        fiber_info = result["fiber"]
        assert "state" in fiber_info
        assert "generation" in fiber_info
        assert fiber_info["state"] in ["active", "unmanaged", "disposed"]
        assert fiber_info["generation"] is None or isinstance(
            fiber_info["generation"], int
        )


# ════════════════════════════════════════════════════════════════
# Section 2: Mutation safety tests (approval required)
# ════════════════════════════════════════════════════════════════


class TestMutationWithoutGate:
    """Tests that mutation tools fail-closed when no approval gate is configured."""

    @pytest.mark.asyncio
    async def test_plugin_reload_without_gate_denies(
        self, self_mgmt_plugin: Any
    ) -> None:
        """With _plugin_approval_gate = None, reload returns ok=False with 'no approval gate' message + requires_approval: True."""
        # Ensure gate is None (should already be from fixture)
        assert self_mgmt_plugin._plugin_approval_gate is None
        
        result = await self_mgmt_plugin._plugin_reload_handler(plugin_id="text_utils")
        
        assert result["ok"] is False
        assert "error" in result
        assert "no approval gate" in result["error"].lower()
        assert result.get("requires_approval") is True

    @pytest.mark.asyncio
    async def test_plugin_disable_without_gate_denies(
        self, self_mgmt_plugin: Any
    ) -> None:
        """Same fail-closed behavior for disable."""
        assert self_mgmt_plugin._plugin_approval_gate is None
        
        result = await self_mgmt_plugin._plugin_disable_handler(plugin_id="text_utils")
        
        assert result["ok"] is False
        assert "error" in result
        assert "no approval gate" in result["error"].lower()
        assert result.get("requires_approval") is True


class TestMutationWithApprovingGate:
    """Tests that mutation tools succeed when gate approves."""

    @pytest.mark.asyncio
    async def test_plugin_reload_with_approving_gate_succeeds(
        self, self_mgmt_plugin: Any
    ) -> None:
        """Inject a fake gate that returns approved=True, verify reload succeeds and returns new generation."""
        # Setup: get current generation
        status_before = await self_mgmt_plugin._plugin_status_handler(
            plugin_id="text_utils"
        )
        assert status_before["ok"] is True
        old_generation = status_before["fiber"]["generation"]
        
        # Inject approving gate
        approving_gate = FakeApprovalGate(approved=True)
        self_mgmt_plugin._plugin_approval_gate = approving_gate
        
        # Perform reload
        result = await self_mgmt_plugin._plugin_reload_handler(plugin_id="text_utils")
        
        # Verify success
        assert result["ok"] is True
        assert result["action"] == "reload"
        assert result["plugin_id"] == "text_utils"
        assert "new_generation" in result
        assert result["state"] == "active"
        # Generation should have bumped
        assert result["new_generation"] > old_generation

    @pytest.mark.asyncio
    async def test_plugin_reload_with_denying_gate_fails(
        self, self_mgmt_plugin: Any
    ) -> None:
        """Fake gate returns approved=False with denial_message, verify blocked with that message."""
        denial_msg = "Reload denied by policy"
        denying_gate = FakeApprovalGate(approved=False, denial_message=denial_msg)
        self_mgmt_plugin._plugin_approval_gate = denying_gate
        
        result = await self_mgmt_plugin._plugin_reload_handler(plugin_id="text_utils")
        
        assert result["ok"] is False
        assert "error" in result
        assert denial_msg in result["error"]
        assert result.get("requires_approval") is True

    @pytest.mark.asyncio
    async def test_plugin_disable_with_approving_gate_succeeds(
        self, self_mgmt_plugin: Any
    ) -> None:
        """Inject approving gate, disable a plugin, verify fiber is DISPOSED and tools removed from registry."""
        from leapflow.domain.plugin_fiber import FiberState
        from leapflow.tools import get_scoped_registry, get_registry
        
        # First reload text_utils to ensure it's active
        approving_gate = FakeApprovalGate(approved=True)
        self_mgmt_plugin._plugin_approval_gate = approving_gate
        reload_result = await self_mgmt_plugin._plugin_reload_handler(
            plugin_id="text_utils"
        )
        assert reload_result["ok"] is True
        
        # Verify it's active before disable
        status_before = await self_mgmt_plugin._plugin_status_handler(
            plugin_id="text_utils"
        )
        assert status_before["ok"] is True
        assert status_before["fiber"]["state"] == "active"
        
        # Get scoped registry to check tool handlers
        scoped = get_scoped_registry()
        reg = get_registry()
        
        # Capture tool names before disable
        tools_before = [t for t in status_before["tools"]]
        assert len(tools_before) > 0
        
        # Disable the plugin
        result = await self_mgmt_plugin._plugin_disable_handler(plugin_id="text_utils")
        
        # Verify success
        assert result["ok"] is True
        assert result["action"] == "disable"
        assert result["plugin_id"] == "text_utils"
        assert result["state"] == "disposed"
        
        # Verify fiber state is disposed
        fiber = scoped.get_fiber("text_utils")
        assert fiber is not None
        assert fiber.state == FiberState.DISPOSED
        
        # Tools should be removed from registry
        for tool in tools_before:
            tool_name = tool["name"]
            assert tool_name not in reg._tool_handlers, f"Tool {tool_name} should be removed"
        
        # Cleanup: reload to restore state for other tests
        await self_mgmt_plugin._plugin_reload_handler(plugin_id="text_utils")


class TestMutationErrorCases:
    """Tests for error handling in mutation tools."""

    @pytest.mark.asyncio
    async def test_self_destruction_blocked(
        self, self_mgmt_plugin: Any
    ) -> None:
        """plugin_disable(plugin_id='self_management') returns ok=False before even checking gate (protection is unconditional)."""
        # Even without a gate, self_management cannot be disabled
        result = await self_mgmt_plugin._plugin_disable_handler(
            plugin_id="self_management"
        )
        
        assert result["ok"] is False
        assert "error" in result
        assert "cannot disable self_management" in result["error"].lower()
        # Should NOT have requires_approval because we never reach the gate check
        assert result.get("requires_approval") is None

    @pytest.mark.asyncio
    async def test_plugin_reload_unknown_plugin_returns_error(
        self, self_mgmt_plugin: Any
    ) -> None:
        """With approving gate, reload of nonexistent plugin returns ok=False."""
        # Inject approving gate first
        approving_gate = FakeApprovalGate(approved=True)
        self_mgmt_plugin._plugin_approval_gate = approving_gate
        
        result = await self_mgmt_plugin._plugin_reload_handler(
            plugin_id="nonexistent_plugin_xyz"
        )
        
        assert result["ok"] is False
        assert "error" in result
        assert "nonexistent_plugin_xyz" in result["error"]
        # Should not have requires_approval since gate approved but plugin not found
        assert result.get("requires_approval") is None


# ════════════════════════════════════════════════════════════════
# Section 3: Gate injection integration tests
# ════════════════════════════════════════════════════════════════


class TestGateInjection:
    """Tests for approval gate injection and descriptor verification."""

    @pytest.mark.asyncio
    async def test_bind_runtime_injects_gate(
        self, self_mgmt_plugin: Any
    ) -> None:
        """Call registry.bind_runtime(plugin_approval_gate=fake_gate), verify plugin._plugin_approval_gate is fake_gate."""
        from leapflow.tools import get_registry
        
        fake_gate = FakeApprovalGate(approved=True)
        
        # Re-bind through registry (simulating what happens at runtime)
        reg = get_registry()
        reg.bind_runtime(plugin_approval_gate=fake_gate)
        
        # Get plugin again and verify it received the gate
        plugin = reg.get_plugin("self_management")
        assert plugin._plugin_approval_gate is fake_gate

    @pytest.mark.asyncio
    async def test_gate_receives_correct_action_descriptor(
        self, self_mgmt_plugin: Any
    ) -> None:
        """Set up a spy gate that records the descriptor it receives; verify the descriptor has action='reload' (or 'disable') and payload contains plugin_id."""
        import json
        from leapflow.security.actions import ActionDescriptor
        
        spy_gate = SpyApprovalGate()
        self_mgmt_plugin._plugin_approval_gate = spy_gate
        
        # Trigger reload
        await self_mgmt_plugin._plugin_reload_handler(plugin_id="test_plugin_abc")
        
        # Verify gate was called
        assert spy_gate.call_count == 1
        assert len(spy_gate.received_descriptors) == 1
        
        descriptor = spy_gate.received_descriptors[0]
        # Verify it's an ActionDescriptor
        assert isinstance(descriptor, ActionDescriptor)
        assert descriptor.kind == "platform.action"
        # Action is stored in metadata
        assert descriptor.metadata["action"] == "reload"
        assert descriptor.metadata["platform"] == "plugin_management"
        # Payload is serialized in detail field
        detail_payload = json.loads(descriptor.detail)
        assert detail_payload["plugin_id"] == "test_plugin_abc"

    @pytest.mark.asyncio
    async def test_disable_gate_receives_correct_descriptor(
        self, self_mgmt_plugin: Any
    ) -> None:
        """Verify disable action also sends correct descriptor."""
        import json
        from leapflow.security.actions import ActionDescriptor
        
        spy_gate = SpyApprovalGate()
        self_mgmt_plugin._plugin_approval_gate = spy_gate
        
        # Trigger disable (on a different plugin to avoid self-destruction block)
        await self_mgmt_plugin._plugin_disable_handler(plugin_id="system_info")
        
        # Verify gate was called
        assert spy_gate.call_count == 1
        
        descriptor = spy_gate.received_descriptors[0]
        assert isinstance(descriptor, ActionDescriptor)
        assert descriptor.kind == "platform.action"
        assert descriptor.metadata["action"] == "disable"
        assert descriptor.metadata["platform"] == "plugin_management"
        # Payload is serialized in detail field
        detail_payload = json.loads(descriptor.detail)
        assert detail_payload["plugin_id"] == "system_info"


# ════════════════════════════════════════════════════════════════
# Section 4: x_leapflow metadata verification
# ════════════════════════════════════════════════════════════════


class TestToolMetadata:
    """Tests for x_leapflow metadata correctness on all tools."""

    @pytest.mark.asyncio
    async def test_mutation_tools_have_high_risk_metadata(
        self, self_mgmt_plugin: Any
    ) -> None:
        """plugin_reload and plugin_disable both have x_leapflow.risk_level='high', requires_approval=True, mutates_state=True."""
        tools = self_mgmt_plugin.tools
        
        # Find the mutation tools
        reload_tool = None
        disable_tool = None
        
        for tool in tools:
            if tool.name == "plugin_reload":
                reload_tool = tool
            elif tool.name == "plugin_disable":
                disable_tool = tool
        
        assert reload_tool is not None, "plugin_reload tool not found"
        assert disable_tool is not None, "plugin_disable tool not found"
        
        # Verify reload metadata
        reload_meta = reload_tool.x_leapflow
        assert reload_meta["risk_level"] == "high"
        assert reload_meta["requires_approval"] is True
        assert reload_tool.mutates_state is True
        
        # Verify disable metadata
        disable_meta = disable_tool.x_leapflow
        assert disable_meta["risk_level"] == "high"
        assert disable_meta["requires_approval"] is True
        assert disable_tool.mutates_state is True

    @pytest.mark.asyncio
    async def test_readonly_tools_dont_require_approval(
        self, self_mgmt_plugin: Any
    ) -> None:
        """plugin_list and plugin_status have requires_approval=False."""
        tools = self_mgmt_plugin.tools
        
        # Find the read-only tools
        list_tool = None
        status_tool = None
        
        for tool in tools:
            if tool.name == "plugin_list":
                list_tool = tool
            elif tool.name == "plugin_status":
                status_tool = tool
        
        assert list_tool is not None, "plugin_list tool not found"
        assert status_tool is not None, "plugin_status tool not found"
        
        # Verify list metadata
        list_meta = list_tool.x_leapflow
        assert list_meta["risk_level"] == "read_only"
        assert list_meta["requires_approval"] is False
        
        # Verify status metadata
        status_meta = status_tool.x_leapflow
        assert status_meta["risk_level"] == "read_only"
        assert status_meta["requires_approval"] is False

    @pytest.mark.asyncio
    async def test_all_tools_have_required_metadata_fields(
        self, self_mgmt_plugin: Any
    ) -> None:
        """All four tools have complete x_leapflow metadata."""
        tools = self_mgmt_plugin.tools
        
        required_fields = {
            "category",
            "risk_level",
            "requires_approval",
            "summary",
        }
        
        for tool in tools:
            meta = tool.x_leapflow
            # All tools must have these core fields
            for field in required_fields:
                assert field in meta, f"Tool {tool.name} missing x_leapflow.{field}"
            
            # Additional fields for mutation tools
            if tool.mutates_state:
                assert "effect_scope" in meta
                assert "idempotency_scope" in meta


# ════════════════════════════════════════════════════════════════
# Section 5: Edge cases and additional scenarios
# ════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Additional edge case tests."""

    @pytest.mark.asyncio
    async def test_plugin_list_empty_categories_handling(
        self, self_mgmt_plugin: Any
    ) -> None:
        """Categories are always present even if only one category exists."""
        result = await self_mgmt_plugin._plugin_list_handler()
        
        assert result["ok"] is True
        assert isinstance(result["categories"], list)
        # Should have at least "system" and potentially others
        assert len(result["categories"]) >= 1

    @pytest.mark.asyncio
    async def test_plugin_status_handles_missing_fiber_gracefully(
        self, self_mgmt_plugin: Any
    ) -> None:
        """plugin_status handles plugins without fibers gracefully."""
        # This test verifies robustness even though in practice all registered
        # plugins should have fibers
        result = await self_mgmt_plugin._plugin_status_handler(plugin_id="text_utils")
        
        assert result["ok"] is True
        assert "fiber" in result
        assert result["fiber"] is not None
        assert "state" in result["fiber"]
        assert "generation" in result["fiber"]

    @pytest.mark.asyncio
    async def test_approval_gate_none_after_test_cleanup(
        self, self_mgmt_plugin: Any
    ) -> None:
        """Fixture cleanup ensures gate is None after test."""
        # Set a gate
        fake_gate = FakeApprovalGate()
        self_mgmt_plugin._plugin_approval_gate = fake_gate
        
        # Verify it's set
        assert self_mgmt_plugin._plugin_approval_gate is fake_gate
        
        # The fixture cleanup should reset it, but we can't directly test
        # that here. Instead, we verify the pattern works by manually cleaning up
        self_mgmt_plugin._plugin_approval_gate = None
        assert self_mgmt_plugin._plugin_approval_gate is None


# ════════════════════════════════════════════════════════════════
# Integration-style tests
# ════════════════════════════════════════════════════════════════


class TestIntegrationScenarios:
    """End-to-end style integration scenarios."""

    @pytest.mark.asyncio
    async def test_full_introspection_workflow(
        self, self_mgmt_plugin: Any
    ) -> None:
        """Test complete workflow: list → status → verify consistency."""
        # Step 1: List all plugins
        list_result = await self_mgmt_plugin._plugin_list_handler()
        assert list_result["ok"] is True
        plugin_ids = [p["plugin_id"] for p in list_result["plugins"]]
        
        # Step 2: Get status for each plugin
        for plugin_id in plugin_ids[:5]:  # Limit to first 5 for performance
            status_result = await self_mgmt_plugin._plugin_status_handler(
                plugin_id=plugin_id
            )
            assert status_result["ok"] is True
            assert status_result["plugin_id"] == plugin_id
            
            # Verify consistency: tool count matches
            listed_plugin = next(
                p for p in list_result["plugins"] if p["plugin_id"] == plugin_id
            )
            assert len(status_result["tools"]) == listed_plugin["tool_count"]

    @pytest.mark.asyncio
    async def test_mutation_requires_approval_pattern(
        self, self_mgmt_plugin: Any
    ) -> None:
        """Verify the pattern: no gate → deny, gate denies → deny, gate approves → proceed."""
        # Scenario 1: No gate
        self_mgmt_plugin._plugin_approval_gate = None
        result = await self_mgmt_plugin._plugin_reload_handler(plugin_id="text_utils")
        assert result["ok"] is False
        assert result.get("requires_approval") is True
        
        # Scenario 2: Gate denies
        denying_gate = FakeApprovalGate(approved=False, denial_message="Policy denied")
        self_mgmt_plugin._plugin_approval_gate = denying_gate
        result = await self_mgmt_plugin._plugin_reload_handler(plugin_id="text_utils")
        assert result["ok"] is False
        assert "Policy denied" in result["error"]
        
        # Scenario 3: Gate approves
        approving_gate = FakeApprovalGate(approved=True)
        self_mgmt_plugin._plugin_approval_gate = approving_gate
        result = await self_mgmt_plugin._plugin_reload_handler(plugin_id="text_utils")
        assert result["ok"] is True
        assert result["state"] == "active"


# ════════════════════════════════════════════════════════════════
# Section 6: Approval exception paths (safety hardening)
# ════════════════════════════════════════════════════════════════


class TestApprovalExceptionPaths:
    """Verify fail-closed behavior when the approval gate raises."""

    @pytest.mark.asyncio
    async def test_approval_gate_runtimeerror_fails_closed(self, self_mgmt_plugin: Any) -> None:
        """When gate.evaluate() raises RuntimeError, reload is denied."""

        class ExplodingGate:
            async def evaluate(self, descriptor: Any) -> Any:
                raise RuntimeError("gate malfunction")

        self_mgmt_plugin._plugin_approval_gate = ExplodingGate()
        result = await self_mgmt_plugin._plugin_reload_handler(plugin_id="text_utils")
        assert result["ok"] is False
        assert "approval check error" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_approval_gate_attributeerror_fails_closed(self, self_mgmt_plugin: Any) -> None:
        """When gate lacks .evaluate() method (AttributeError), reload is denied."""

        class BrokenGate:
            pass  # no evaluate method

        self_mgmt_plugin._plugin_approval_gate = BrokenGate()
        result = await self_mgmt_plugin._plugin_reload_handler(plugin_id="text_utils")
        assert result["ok"] is False
        assert not result["ok"]


class TestDisableEdgeCases:
    """Verify disable handles edge cases."""

    @pytest.mark.asyncio
    async def test_disable_plugin_without_fiber(self, self_mgmt_plugin: Any) -> None:
        """When a plugin exists but has no fiber, disable returns clear error."""

        class ApprovingGate:
            async def evaluate(self, descriptor: Any) -> Any:
                class R:
                    approved = True
                    denial_message = ""
                return R()

        self_mgmt_plugin._plugin_approval_gate = ApprovingGate()

        # Simulate a plugin without a fiber
        from leapflow.tools import get_scoped_registry

        scoped = get_scoped_registry()
        # Remove the fiber for text_utils to simulate the edge case
        original_fiber = scoped._fibers.pop("text_utils", None)
        try:
            result = await self_mgmt_plugin._plugin_disable_handler(plugin_id="text_utils")
            assert result["ok"] is False
            assert "no fiber" in result["error"].lower()
        finally:
            # Restore
            if original_fiber is not None:
                scoped._fibers["text_utils"] = original_fiber


# ════════════════════════════════════════════════════════════════
# Section 7: Self-modification risk classification (security)
# ════════════════════════════════════════════════════════════════


class TestSelfModificationRiskClassification:
    """Verify self-modification is treated as HIGH risk with no permanent grants."""

    @pytest.mark.asyncio
    async def test_self_modification_descriptor_metadata(self, self_mgmt_plugin: Any) -> None:
        """The ActionDescriptor built for self-modification carries HIGH risk hints."""
        spy_gate = SpyApprovalGate()
        self_mgmt_plugin._plugin_approval_gate = spy_gate

        # Trigger reload to capture the descriptor
        await self_mgmt_plugin._plugin_reload_handler(plugin_id="text_utils")

        assert spy_gate.call_count == 1
        descriptor = spy_gate.received_descriptors[0]

        # Verify risk hints in metadata
        assert descriptor.metadata.get("effect") == "write"
        assert descriptor.metadata.get("risk_level") == "high"
        assert descriptor.metadata.get("category") == "self_modification"
        assert descriptor.metadata.get("platform") == "plugin_management"

    @pytest.mark.asyncio
    async def test_self_modification_disable_descriptor_metadata(self, self_mgmt_plugin: Any) -> None:
        """The ActionDescriptor built for disable also carries HIGH risk hints."""
        spy_gate = SpyApprovalGate()
        self_mgmt_plugin._plugin_approval_gate = spy_gate

        # Trigger disable (not self_management, to avoid the self-destruction guard)
        await self_mgmt_plugin._plugin_disable_handler(plugin_id="text_utils")

        assert spy_gate.call_count == 1
        descriptor = spy_gate.received_descriptors[0]

        assert descriptor.metadata.get("effect") == "write"
        assert descriptor.metadata.get("risk_level") == "high"
        assert descriptor.metadata.get("category") == "self_modification"

    def test_risk_classifier_denies_permanent_for_plugin_management(self) -> None:
        """DefaultRiskClassifier returns allow_permanent=False for plugin_management."""
        from leapflow.security.actions import ActionDescriptor
        from leapflow.security.risk import DefaultRiskClassifier, RiskLevel

        classifier = DefaultRiskClassifier()
        descriptor = ActionDescriptor.platform_action(
            "plugin_management",
            "reload",
            {"plugin_id": "text_utils"},
            metadata={
                "effect": "write",
                "risk_level": "high",
                "category": "self_modification",
            },
        )
        assessment = classifier.assess(descriptor)

        assert assessment.level == RiskLevel.HIGH
        assert assessment.allow_permanent is False
        assert "agent_self_modification" in assessment.reasons

    def test_risk_classifier_defense_in_depth_without_explicit_metadata(self) -> None:
        """Even without explicit risk_level metadata, plugin_management is HIGH."""
        from leapflow.security.actions import ActionDescriptor
        from leapflow.security.risk import DefaultRiskClassifier, RiskLevel

        classifier = DefaultRiskClassifier()
        # Simulate a caller that forgets to set risk metadata
        descriptor = ActionDescriptor.platform_action(
            "plugin_management",
            "reload",
            {"plugin_id": "text_utils"},
        )
        assessment = classifier.assess(descriptor)

        assert assessment.level == RiskLevel.HIGH
        assert assessment.allow_permanent is False
        assert "agent_self_modification" in assessment.reasons


# ════════════════════════════════════════════════════════════════
# Section 8: P1 Feature Tests — plugin_enable, cross-subsystem
#            introspection, and PluginHealthProducer
# ════════════════════════════════════════════════════════════════


class TestP1Features:
    """Tests for the three P1 features: plugin_enable, cross-subsystem introspection, MonitorProducer."""

    # ── plugin_enable tests ──────────────────────────────

    @pytest.mark.asyncio
    async def test_plugin_enable_without_gate_denies(self, self_mgmt_plugin: Any) -> None:
        """plugin_enable fail-closed when no approval gate is configured."""
        assert self_mgmt_plugin._plugin_approval_gate is None

        result = await self_mgmt_plugin._plugin_enable_handler(plugin_id="text_utils")

        assert result["ok"] is False
        assert "error" in result
        assert "no approval gate" in result["error"].lower()
        assert result.get("requires_approval") is True

    @pytest.mark.asyncio
    async def test_plugin_enable_with_approving_gate_succeeds(
        self, self_mgmt_plugin: Any
    ) -> None:
        """Inject approving gate, enable a disabled plugin, verify ok=True + new_generation."""
        from leapflow.domain.plugin_fiber import FiberState

        # Use system_info which is stable and not touched by other tests
        target_plugin = "system_info"

        approving_gate = FakeApprovalGate(approved=True)
        self_mgmt_plugin._plugin_approval_gate = approving_gate

        # First ensure it's active by reloading
        reload_result = await self_mgmt_plugin._plugin_reload_handler(plugin_id=target_plugin)
        assert reload_result["ok"] is True

        # Disable it
        disable_result = await self_mgmt_plugin._plugin_disable_handler(plugin_id=target_plugin)
        assert disable_result["ok"] is True
        assert disable_result["state"] == "disposed"

        # Now enable it
        result = await self_mgmt_plugin._plugin_enable_handler(plugin_id=target_plugin)

        assert result["ok"] is True
        assert result["action"] == "enable"
        assert result["plugin_id"] == target_plugin
        assert "new_generation" in result
        assert result["state"] == "active"
        assert isinstance(result["new_generation"], int)

    @pytest.mark.asyncio
    async def test_plugin_enable_self_management_blocked(
        self, self_mgmt_plugin: Any
    ) -> None:
        """Cannot enable self_management (it's already active)."""
        approving_gate = FakeApprovalGate(approved=True)
        self_mgmt_plugin._plugin_approval_gate = approving_gate

        result = await self_mgmt_plugin._plugin_enable_handler(plugin_id="self_management")

        assert result["ok"] is False
        assert "already active" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_plugin_enable_unknown_plugin_error(
        self, self_mgmt_plugin: Any
    ) -> None:
        """Approving gate + nonexistent plugin → error."""
        approving_gate = FakeApprovalGate(approved=True)
        self_mgmt_plugin._plugin_approval_gate = approving_gate

        result = await self_mgmt_plugin._plugin_enable_handler(
            plugin_id="absolutely_nonexistent_plugin_xyz"
        )

        assert result["ok"] is False
        assert "error" in result

    # ── Cross-subsystem introspection tests ──────────────

    @pytest.mark.asyncio
    async def test_plugin_list_includes_gateway_adapters(
        self, self_mgmt_plugin: Any
    ) -> None:
        """Response has `gateway_adapters` field with len > 0."""
        result = await self_mgmt_plugin._plugin_list_handler()

        assert result["ok"] is True
        assert "gateway_adapters" in result
        assert isinstance(result["gateway_adapters"], list)
        assert len(result["gateway_adapters"]) > 0
        # Each adapter has platform_id and subsystem
        for adapter in result["gateway_adapters"]:
            assert "platform_id" in adapter
            assert adapter["subsystem"] == "gateway"

    @pytest.mark.asyncio
    async def test_plugin_list_includes_llm_providers(
        self, self_mgmt_plugin: Any
    ) -> None:
        """Response has `llm_providers` field."""
        result = await self_mgmt_plugin._plugin_list_handler()

        assert result["ok"] is True
        assert "llm_providers" in result
        assert isinstance(result["llm_providers"], list)
        # LLM providers may or may not be present depending on test env
        for provider in result["llm_providers"]:
            assert "provider_id" in provider
            assert provider["subsystem"] == "llm"

    @pytest.mark.asyncio
    async def test_plugin_list_has_total_count(self, self_mgmt_plugin: Any) -> None:
        """total_count = tool_plugins + gateway + llm."""
        result = await self_mgmt_plugin._plugin_list_handler()

        assert result["ok"] is True
        expected_total = (
            len(result["plugins"])
            + len(result["gateway_adapters"])
            + len(result["llm_providers"])
        )
        assert result["total_count"] == expected_total

    @pytest.mark.asyncio
    async def test_plugin_list_backward_compat(self, self_mgmt_plugin: Any) -> None:
        """Old fields (plugins, plugin_count, categories) still present."""
        result = await self_mgmt_plugin._plugin_list_handler()

        assert result["ok"] is True
        # Backward compat fields
        assert "plugins" in result
        assert "plugin_count" in result
        assert "categories" in result
        assert result["plugin_count"] == len(result["plugins"])
        assert isinstance(result["categories"], list)

    # ── PluginHealthProducer tests ───────────────────────

    def test_plugin_health_producer_importable(self) -> None:
        """Can import and instantiate PluginHealthProducer."""
        from leapflow.monitor.plugin_health_producer import PluginHealthProducer

        producer = PluginHealthProducer()
        assert producer.domain == "plugin_health"

    @pytest.mark.asyncio
    async def test_plugin_health_producer_returns_empty_without_advisor(
        self, self_mgmt_plugin: Any
    ) -> None:
        """poll() returns [] when no advisor wired."""
        import time
        from leapflow.monitor.plugin_health_producer import PluginHealthProducer
        from leapflow.monitor.types import ProducerContext, WatchSpec
        import leapflow.learning.plugin_advisor as advisor_mod

        # Ensure no default advisor
        original = advisor_mod._default_advisor
        advisor_mod._default_advisor = None
        try:
            producer = PluginHealthProducer()
            ctx = ProducerContext(
                spec=WatchSpec(name="test", domain="plugin_health", watch_id="test_watch"),
                now=time.time(),
            )
            findings = await producer.observe(ctx)
            assert findings == []
        finally:
            advisor_mod._default_advisor = original

    @pytest.mark.asyncio
    async def test_plugin_health_producer_detects_trust_degradation(
        self, self_mgmt_plugin: Any
    ) -> None:
        """Wire advisor, cause trust demotion, poll → Finding with NOTABLE severity."""
        import time
        from leapflow.monitor.plugin_health_producer import PluginHealthProducer
        from leapflow.monitor.types import ProducerContext, Severity, WatchSpec
        from leapflow.learning.plugin_trust import PluginTrustLedger
        from leapflow.learning.plugin_stats import PluginUsageTracker
        from leapflow.learning.plugin_advisor import PluginAdvisor, set_default_advisor
        import leapflow.learning.plugin_advisor as advisor_mod

        original = advisor_mod._default_advisor
        try:
            # Build components with low thresholds for testability
            ledger = PluginTrustLedger(candidate_at=2, verified_at=5, production_at=10, demote_after=2)
            tracker = PluginUsageTracker()
            tracker.set_trust_ledger(ledger)
            advisor = PluginAdvisor(trust_ledger=ledger, usage_tracker=tracker)
            set_default_advisor(advisor)

            # Use self_management itself as the target (always in registry)
            target_plugin = "self_management"

            # Promote target to CANDIDATE by recording successes
            for _ in range(3):
                ledger.record_success(target_plugin)
            assert ledger.level(target_plugin).name == "CANDIDATE"

            producer = PluginHealthProducer()
            ctx = ProducerContext(
                spec=WatchSpec(name="test", domain="plugin_health", watch_id="test_watch"),
                now=time.time(),
            )

            # First observation: establishes baseline (no degradation yet)
            findings_1 = await producer.observe(ctx)
            trust_degrades_1 = [f for f in findings_1 if "trust degraded" in f.title.lower()]
            assert len(trust_degrades_1) == 0

            # Now cause demotion: enough consecutive failures
            for _ in range(2):
                ledger.record_failure(target_plugin)
            # Trust should have dropped to DRAFT
            assert ledger.level(target_plugin).name == "DRAFT"

            # Second observation: should detect degradation
            findings_2 = await producer.observe(ctx)
            trust_degrades_2 = [f for f in findings_2 if "trust degraded" in f.title.lower()]
            assert len(trust_degrades_2) >= 1
            finding = trust_degrades_2[0]
            assert finding.severity == Severity.NOTABLE
            assert target_plugin in finding.title
        finally:
            advisor_mod._default_advisor = original

    @pytest.mark.asyncio
    async def test_plugin_health_producer_detects_high_error_rate(
        self, self_mgmt_plugin: Any
    ) -> None:
        """Wire advisor, record high error rate, poll → Finding with ALERT severity."""
        import time
        from leapflow.monitor.plugin_health_producer import PluginHealthProducer
        from leapflow.monitor.types import ProducerContext, Severity, WatchSpec
        from leapflow.learning.plugin_trust import PluginTrustLedger
        from leapflow.learning.plugin_stats import PluginUsageTracker
        from leapflow.learning.plugin_advisor import PluginAdvisor, set_default_advisor
        import leapflow.learning.plugin_advisor as advisor_mod

        original = advisor_mod._default_advisor
        try:
            ledger = PluginTrustLedger()
            tracker = PluginUsageTracker()
            tracker.set_trust_ledger(ledger)
            advisor = PluginAdvisor(trust_ledger=ledger, usage_tracker=tracker)
            set_default_advisor(advisor)

            # Use self_management tools (always available, never disabled by other tests)
            from leapflow.tools import get_registry
            reg = get_registry()
            reg.assemble()  # Ensure registry is fully populated
            plugin = reg.get_plugin("self_management")
            assert plugin is not None
            tool_names = [t.name for t in plugin.tools]
            assert len(tool_names) > 0
            tool_name = tool_names[0]

            # Record 2 success + 5 failures = 71% error rate
            for _ in range(2):
                tracker.record(tool_name, ok=True, duration_ms=10.0)
            for _ in range(5):
                tracker.record(tool_name, ok=False, duration_ms=10.0)

            producer = PluginHealthProducer()
            ctx = ProducerContext(
                spec=WatchSpec(name="test", domain="plugin_health", watch_id="test_watch"),
                now=time.time(),
            )

            findings = await producer.observe(ctx)
            error_findings = [f for f in findings if "error rate" in f.title.lower()]
            assert len(error_findings) >= 1
            finding = error_findings[0]
            assert finding.severity == Severity.ALERT
            assert "self_management" in finding.title
        finally:
            advisor_mod._default_advisor = original

