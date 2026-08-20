"""ToolPlugin Protocol — the unified contract for tool plugin modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable


@runtime_checkable
class ToolPlugin(Protocol):
    """Tool plugin protocol.

    Each plugin module implements this Protocol to register its tool set
    with the ToolPluginRegistry. The Registry assembles all tools' OpenAI
    schemas and handler mappings in a single assemble() pass.
    """

    @property
    def plugin_id(self) -> str:
        """Unique plugin identifier, e.g. 'file_operations', 'shell_terminal'.

        Used for debug logging, runtime dependency resolution, and conflict detection.
        """
        ...

    @property
    def category(self) -> str:
        """Tool category label for PCD capability_expand and grouped display.

        Must be consistent with x_leapflow.category.
        """
        ...

    @property
    def tools(self) -> list[ToolMetadata]:
        """List of all tool metadata registered by this plugin.

        ToolMetadata is the single source of truth (SSOT).
        """
        ...

    @property
    def dependencies(self) -> list[str]:
        """List of runtime dependency names required by this plugin.

        The Registry distributes deps matching this list during bind_runtime().
        Example: ['memory_manager', 'research_ledger', 'file_read_gate']
        """
        ...

    def bind_runtime(self, **deps: Any) -> None:
        """Receive runtime-injected dependencies.

        Called by ToolPluginRegistry.bind_runtime() uniformly.
        Plugins should ignore kwargs not declared in their dependencies.
        """
        ...


@dataclass(frozen=True)
class ToolMetadata:
    """Unified tool metadata — Single Source of Truth for each tool.

    A tool only needs to define ToolMetadata once to:
    - Generate OpenAI function-calling schema (consumed by LLM)
    - Provide handler mapping (dispatched by engine)
    - Carry PCD / capability metadata (consumed by context_disclosure)
    """

    name: str
    description: str
    parameters_schema: dict[str, Any]  # OpenAI JSON Schema format
    handler: Callable[..., Any]
    # Runtime dispatch goes through leapflow.plugins.handler_invocation.invoke_tool_handler,
    # which supports both generated-plugin **kwargs handlers and older params-dict handlers.
    x_leapflow: dict[str, Any] = field(default_factory=dict)
    mutates_state: bool = False

    def to_openai_schema(self) -> dict[str, Any]:
        """Generate OpenAI function-calling schema dict.

        ``mutates_state`` is folded into ``x_leapflow`` so schema-only
        consumers (e.g. ToolRegistry.from_definitions) can classify
        side-effecting tools without access to the metadata object.
        """
        x_leapflow = dict(self.x_leapflow)
        if self.mutates_state:
            x_leapflow.setdefault("mutates_state", True)
        entry: dict[str, Any] = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }
        if x_leapflow:
            entry["function"]["x_leapflow"] = x_leapflow
        return entry
