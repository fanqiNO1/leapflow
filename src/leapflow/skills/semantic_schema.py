"""Semantic tool schema conversion — expose ToolBridge semantic tools to the LLM.

The unified tool loop discloses tools from OpenAI function-calling schemas,
while semantic desktop tools (observe_ui, click, switch_app, ...) are only
registered on the ToolBridge as ``ToolDefinition`` objects with free-form
parameter strings. This module bridges the two representations:

- ``SEMANTIC_TOOL_NAMES``: the fixed set of SemanticAdapter-backed tools that
  may be disclosed (ToolBridge defaults such as file_list/shell/launch_app are
  excluded — they overlap with the gp_* catalog).
- ``parse_param_spec``: parses bridge parameter strings, e.g.
  ``"string (optional, default=30) — max seconds to wait"``.
- ``build_semantic_schemas``: converts whatever semantic tools are currently
  registered on a bridge into OpenAI schemas carrying ``x_leapflow`` metadata.
  A bridge without semantic tools (perception offline) yields an empty list,
  which makes the bridge itself the dynamic on/off switch.
- ``build_semantic_handlers``: the dispatch-side counterpart — extracts the
  bridge's own async handlers for the semantic tools so the unified tool loop
  can merge them into its per-turn handler table. Schemas and handlers are
  built from the same bridge snapshot, so disclosure and execution never
  disagree about which desktop tools exist.

Risk metadata is declared explicitly per tool: the disclosure planner treats
missing metadata as fail-closed for core admission, and ``desktop`` is not a
category its risk table recognizes, so every entry states category, risk,
schema cost, and approval requirement. All desktop tools are non-core
(schema_cost="high"); the model obtains them via ``capability_expand``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Protocol, runtime_checkable

# Parameter spec head, e.g. "string" or "number (optional, default=30)".
_HEAD_RE = re.compile(r"^(?P<type>[A-Za-z]+)\s*(?:\((?P<flags>[^)]*)\))?$")

# JSON schema types the bridge registration format uses; anything else maps to string.
_TYPE_MAP = {"string": "string", "number": "number", "boolean": "boolean", "int": "integer"}


@dataclass(frozen=True)
class ParamSpec:
    """Parsed bridge parameter description."""

    type: str
    required: bool
    description: str


@runtime_checkable
class ToolDefinitionsSource(Protocol):
    """Anything that can list ToolDefinition objects (e.g. ToolBridge)."""

    def tool_definitions(self) -> List[Any]: ...

    @property
    def handlers(self) -> Dict[str, Any]: ...


SEMANTIC_TOOL_NAMES: FrozenSet[str] = frozenset({
    "observe_ui",
    "click",
    "type_text",
    "shortcut",
    "switch_app",
    "list_apps",
    "open_url",
    "get_clipboard",
    "set_clipboard",
    "read_text",
    "wait",
    "wait_until",
    "wait_until_stable",
    "scroll",
    "select_text",
    "right_click",
    "screenshot",
})

_OBSERVATION_TOOLS: FrozenSet[str] = frozenset({
    "observe_ui", "list_apps", "read_text", "get_clipboard", "screenshot",
})
_WAIT_TOOLS: FrozenSet[str] = frozenset({
    "wait", "wait_until", "wait_until_stable",
})
_MUTATING_TOOLS: FrozenSet[str] = frozenset({
    "click", "type_text", "shortcut", "switch_app", "open_url",
    "set_clipboard", "scroll", "select_text", "right_click",
})

DESKTOP_CATEGORY = "desktop"


def _metadata_for(name: str) -> Dict[str, Any]:
    """Build x_leapflow metadata for one semantic tool.

    Observation and wait tools are read-only and run without approval;
    mutating tools are medium-risk and gated by the desktop approval gate.
    schema_cost is high for every desktop tool so none qualifies as core
    (core admission requires read_only risk AND non-high schema cost).
    """
    if name in _MUTATING_TOOLS:
        risk, approval = "medium", True
    else:
        risk, approval = "read_only", False
    return {
        "category": DESKTOP_CATEGORY,
        "risk_level": risk,
        "schema_cost": "high",
        "requires_approval": approval,
    }


def semantic_requires_approval(name: str) -> bool:
    """Return whether executing this semantic tool requires desktop approval."""
    return name in _MUTATING_TOOLS


def parse_param_spec(spec: str) -> ParamSpec:
    """Parse a bridge parameter string into a structured spec.

    Accepts the registration format used by bridge_factory, e.g.
    ``"string (required) — text to type"`` or ``"number (optional, default=30)
    — max seconds to wait"``. Tolerates a missing flags group, a missing
    description, and empty input.
    """
    text = (spec or "").strip()
    if not text:
        return ParamSpec(type="string", required=False, description="")

    head, sep, description = text.partition("\u2014")
    head = head.strip()
    description = description.strip() if sep else ""

    match = _HEAD_RE.match(head)
    if match is None:
        return ParamSpec(type="string", required=False, description=text)

    raw_type = match.group("type").lower()
    flags = (match.group("flags") or "").lower()
    required = "required" in flags and "optional" not in flags
    return ParamSpec(
        type=_TYPE_MAP.get(raw_type, "string"),
        required=required,
        description=description,
    )


def semantic_tool_to_openai(definition: Any) -> Optional[Dict[str, Any]]:
    """Convert one ToolDefinition into an OpenAI function schema.

    Returns None for tools outside SEMANTIC_TOOL_NAMES so callers can pass
    arbitrary bridge definitions without pre-filtering.
    """
    name = str(getattr(definition, "name", "") or "")
    if name not in SEMANTIC_TOOL_NAMES:
        return None

    parameters = getattr(definition, "parameters", None) or {}
    properties: Dict[str, Any] = {}
    required_names: List[str] = []
    for param_name, param_spec in parameters.items():
        parsed = parse_param_spec(str(param_spec))
        properties[str(param_name)] = {
            "type": parsed.type,
            "description": parsed.description,
        }
        if parsed.required:
            required_names.append(str(param_name))

    schema: Dict[str, Any] = {
        "type": "function",
        "function": {
            "name": name,
            "description": str(getattr(definition, "description", "") or ""),
            "parameters": {
                "type": "object",
                "properties": properties,
            },
        },
        "x_leapflow": _metadata_for(name),
    }
    if required_names:
        schema["function"]["parameters"]["required"] = required_names
    return schema


def build_semantic_schemas(bridge: Optional[ToolDefinitionsSource]) -> List[Dict[str, Any]]:
    """Collect OpenAI schemas for the semantic tools registered on a bridge.

    Returns an empty list when the bridge is None or carries no semantic
    tools (perception offline), which is the signal for the unified tool
    catalog to omit the desktop category entirely. Output order is sorted by
    tool name for deterministic disclosure.
    """
    if bridge is None:
        return []

    schemas: List[Dict[str, Any]] = []
    try:
        definitions = bridge.tool_definitions()
    except (AttributeError, TypeError, RuntimeError):
        return []

    for definition in definitions:
        schema = semantic_tool_to_openai(definition)
        if schema is not None:
            schemas.append(schema)
    schemas.sort(key=lambda item: item["function"]["name"])
    return schemas


def build_semantic_handlers(bridge: Optional[ToolDefinitionsSource]) -> Dict[str, Any]:
    """Collect the bridge's own handlers for registered semantic tools.

    Dispatch-side counterpart of ``build_semantic_schemas``: returns an empty
    dict when the bridge is None or carries no semantic tools (perception
    offline). Handlers are the bridge's native callables (the same ones its
    ``dispatch`` invokes), so merging them into the unified loop's handler
    table executes desktop actions through the SemanticAdapter exactly as the
    skill executor would. Only SEMANTIC_TOOL_NAMES are included — bridge
    defaults (file_list, shell, launch_app, ...) stay out to avoid shadowing
    the gp_* catalog.
    """
    if bridge is None:
        return {}

    try:
        all_handlers = bridge.handlers
    except AttributeError:
        return {}

    return {
        name: handler
        for name, handler in all_handlers.items()
        if name in SEMANTIC_TOOL_NAMES and handler is not None
    }
