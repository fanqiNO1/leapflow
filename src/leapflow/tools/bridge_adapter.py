"""ToolBridge compatibility adapter.

Encapsulates all gp_ prefix logic and ToolBridge registration format.
This is a legacy compat layer — remove when ToolBridge is eliminated from engine dispatch.

## Removal Assessment (as of this session)

This section captures an in-repo evaluation of whether the ``gp_`` prefix and
the ``ToolBridgeAdapter`` can be retired cleanly. It is an assessment only
(no removal is performed here); it documents the exact dependency surface,
the migration steps, and the risks so a follow-up cleanup can proceed with
confidence.

### 1. Is ToolBridge still in the critical dispatch path?

Yes — today. ``AgentEngine._execute_general_tool`` (see
``src/leapflow/engine/engine.py`` around line 4820) has three ordered branches:

    0. Semantic desktop tools (SEMANTIC_TOOL_NAMES) — admitted only when
       the per-turn handler table carries them, gated by the desktop
       approval gate. These tools live ONLY on the bridge (registered by
       ``skills.bridge_factory.build_tool_bridge``), never in
       ``ToolPluginRegistry``.
    1. ToolBridge dispatch with ``gp_``-prefixed name (primary path today).
    2. ToolBridge dispatch with the exact name (also via the bridge).
    3. Fallback: ``handlers.get(name)`` where ``handlers`` is built by
       ``AgentEngine._resolve_handlers`` as
       ``dict(registry.tool_handlers) | build_semantic_handlers(bridge)``.

So removing ToolBridge as the *primary* path only re-routes to branch (3),
which already contains the union of registry handlers and semantic-tool
handlers extracted from the bridge — the handler table is already the full
superset that the bridge would dispatch to. The bridge itself is a
single-instance in-process function-call registry, not a network hop or
protocol boundary; there is nothing intrinsically "transport-level" about
it.

However, semantic tools are still *registered* on the bridge object today.
Even if dispatch stops going through it, the bridge is the only place they
live, so ``build_semantic_handlers(bridge)`` still needs SOMETHING to enumerate.
Any cleanup must either keep a slim bridge as a semantic-tool container or
port the SemanticAdapter registrations into a ToolPlugin.

### 2. What depends on the ``gp_`` prefix?

Production code paths:

- ``engine.engine._execute_general_tool``: prefixes ``gp_`` for its primary
  bridge dispatch call.
- ``engine.engine._concurrency_spec_lookup``: strips ``gp_`` before spec
  lookup — purely a normalization convenience.
- ``engine.engine._post_process_tool_result``: strips ``gp_`` before reading
  tool manifest metadata — also normalization.
- ``engine.engine`` platform_action deprecation set:
  ``{"platform_action", "gp_platform_action"}`` — tolerates both spellings.
- ``engine.engine`` prompt text: ``"Use ``gp_skills_list`` to browse or
  ``gp_skill_view`` to read details"`` — exposed to the LLM.
- ``tools.plugin_registry.assemble``: calls ``apply_gp_aliases()`` after
  assembly.
- ``tools.plugin_registry.remove_plugin_handlers``: pops the ``gp_`` alias
  alongside the plain name.
- ``tools.scoped_registry.scoped_register_late_tool``: writes the ``gp_``
  alias for late-registered tools.
- ``tools.__init__.bootstrap_tools`` -> ``ToolBridgeAdapter.bootstrap``:
  registers every plugin tool on the bridge with a ``gp_`` prefix; called
  from ``cli.context`` during engine construction and host reconfiguration.
- ``tools.plugins.self_management._plugin_install_handler`` (this session):
  mirrors the alias invariant when installing a freshly generated plugin.

Recovery / classification helpers keep ``gp_`` in tool_name strings so that
failure envelopes and audit rows retain the exact name the engine dispatched
under. This is cosmetic, not structural — no logic branches on the prefix.

Test surface (assertions to update):

- ``tests/test_plugin_reload.py`` — asserts the ``gp_`` alias lifecycle.
- ``tests/test_scoped_registry.py`` — asserts ``gp_`` alias cleanup on reload.
- ``tests/test_web_fetch.py`` — asserts ``gp_web_fetch`` in TOOL_HANDLERS.
- ``tests/test_adaptive_depth.py`` — uses ``gp_delegate_task`` in filter fixtures.
- ``tests/test_semantic_schema.py`` — asserts ``gp_shell_run`` maps to None.
- ``tests/test_tool_concurrency.py`` — asserts the ``gp_``-strip fallback works.
- ``tests/test_recovery_coordinator.py`` — uses ``gp_web_search`` in fixtures.

### 3. Exact steps + risks to remove

Recommended migration (three landings, each independently green):

Landing A — stop *depending* on the ``gp_`` prefix in dispatch:
    1. In ``_execute_general_tool``, prefer ``handlers.get(name)`` before
       falling back to bridge dispatch. Because ``_resolve_handlers`` already
       merges semantic handlers in, this covers today's branch (0)/(1)/(2)
       for every tool the bridge could have found. Keep the bridge branches
       as a temporary fallback so behavior is unchanged when the merged
       handler table is missing an entry.
    2. Add a runtime warning when the fallback (bridge dispatch) fires —
       any warning in the offline journey suite = a tool that lives only
       on the bridge.
    3. Remove the ``prefixed = f"gp_{name}"`` primary call. The exact-name
       bridge branch keeps semantic tools reachable during transition.

Landing B — remove the alias-generation surface:
    1. Delete ``ToolBridgeAdapter.apply_gp_aliases`` and its callers in
       ``ToolPluginRegistry.assemble``, ``ScopedToolRegistry.scoped_register_late_tool``,
       and ``ToolPluginRegistry.remove_plugin_handlers`` (drop the ``gp_``
       pop).
    2. Remove the alias mirror inside ``self_management._plugin_install_handler``.
    3. Update the engine prompt text to drop ``gp_skills_list``/``gp_skill_view``.
    4. Update the six affected tests to assert only the plain names.
    5. Keep the ``removeprefix("gp_")`` normalization helpers — they are
       harmless and forward-compatible with any historic audit rows.

Landing C — delete the bridge itself as a dispatch surface:
    1. Port ``skills.bridge_factory.build_tool_bridge`` into a ToolPlugin
       (``desktop_semantic`` or similar) that declares ``execution`` and
       ``perception`` as dependencies and exposes the SEMANTIC_TOOL_NAMES
       as ordinary ToolMetadata entries. Its ``bind_runtime`` wires the
       SemanticAdapter.
    2. Delete ``build_semantic_handlers`` and the semantic-tool branch of
       ``_execute_general_tool``; ``handlers.get(name)`` is now sufficient.
    3. Delete ``ToolBridgeAdapter``, ``bootstrap_tools``, and the
       ``_tool_bridge`` field on ``AgentEngine`` / ``LeapFlowContext``.
    4. Delete ``skills.tool_executor.ToolBridge`` (still used by the
       ``ToolUseSkillExecutor`` fallback path around engine line 5350;
       migrate that fallback to the plugin registry first).

### 4. Is it safe to remove now?

No — not in one landing, and not without the engine changes above. The
bridge is still the only *registration site* for SemanticAdapter tools, so a
single-shot removal would silently drop desktop control. The alias half of
the surface (``gp_``) is nearly ready: it costs one engine dispatch edit,
one prompt-text edit, and six test updates. The bridge half needs the
porting work in Landing C before it can go.

Recommended order: Landing A (safe, behavior-preserving) → Landing B (small
test churn, no behavior change) → Landing C (real refactor, gated on desktop
coverage in the real journeys). Each landing is independently mergeable and
leaves the system in a working state.
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from leapflow.tools.plugin_registry import ToolPluginRegistry
    from leapflow.tools.protocol import ToolMetadata

logger = logging.getLogger(__name__)


class ToolBridgeAdapter:
    """Adapts ToolPluginRegistry output to legacy ToolBridge format.

    Responsibilities:
    - Generate gp_ prefixed aliases for all assembled handlers
    - Produce bridge-format tool entries consumed by ToolRegistry.from_definitions
    - Bootstrap a ToolBridge instance with gp_-prefixed tool registrations

    Lifecycle:
    - Created with a reference to the ToolPluginRegistry
    - apply_gp_aliases() called after registry.assemble() to inject aliases into handlers
    - bridge_tools / bootstrap() used by engine and legacy paths
    """

    def __init__(self, registry: "ToolPluginRegistry") -> None:
        self._registry = registry

    def apply_gp_aliases(self) -> None:
        """Generate gp_ prefixed aliases in the registry's tool_handlers.

        Must be called after registry assembly. Creates bidirectional aliases:
        - gp_X → X (for tools declared with gp_ prefix)
        - X → gp_X (for all other tools)
        """
        handlers = self._registry._tool_handlers

        # Direction 1: gp_X exists → ensure unprefixed X also exists
        for name in list(handlers.keys()):
            if name.startswith("gp_"):
                unprefixed = name[3:]
                if unprefixed not in handlers:
                    handlers[unprefixed] = handlers[name]

        # Direction 2: plain name exists → ensure gp_X also exists
        for name in list(handlers.keys()):
            if not name.startswith("gp_"):
                prefixed = f"gp_{name}"
                if prefixed not in handlers:
                    handlers[prefixed] = handlers[name]

    def apply_late_tool_alias(self, name: str, handler: Any) -> None:
        """Register gp_ alias for a late-registered tool.

        Called by register_late_tool to maintain alias invariant after assembly.
        """
        self._registry._tool_handlers[f"gp_{name}"] = handler

    @property
    def bridge_tools(self) -> list[dict[str, Any]]:
        """Bridge-format tool list (for ToolRegistry.from_definitions compatibility).

        Returns list of dicts with gp_-prefixed names, description, parameters,
        handler, and mutates_state — the format ToolRegistry.from_definitions expects.
        """
        result: list[dict[str, Any]] = []
        if not self._registry._assembled:
            return result
        for meta in self._registry._all_metadata:
            result.append({
                "name": f"gp_{meta.name}",
                "description": meta.description,
                "parameters": meta.parameters_schema,
                "handler": meta.handler,
                "mutates_state": meta.mutates_state,
            })
        return result

    def bootstrap(self, bridge: Any) -> int:
        """Register all tools on a ToolBridge instance with gp_ prefix.

        Legacy ToolBridge compat — the engine's _execute_general_tool dispatches
        through ToolBridge with gp_-prefixed names.

        Returns:
            Number of tools successfully registered.
        """
        reg = self._registry
        if not reg._assembled:
            reg.assemble()
        registered = 0
        for meta in reg.all_metadata:
            try:
                bridge.register(
                    f"gp_{meta.name}",
                    meta.description,
                    meta.parameters_schema,
                    meta.handler,
                    mutates_state=meta.mutates_state,
                )
                registered += 1
            except (TypeError, ValueError, AttributeError):
                pass
        return registered
