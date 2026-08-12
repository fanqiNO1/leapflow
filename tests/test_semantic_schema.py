"""Tests for the semantic desktop tool schema conversion layer."""

from __future__ import annotations

from leapflow.skills.semantic_schema import (
    DESKTOP_CATEGORY,
    SEMANTIC_TOOL_NAMES,
    build_semantic_handlers,
    build_semantic_schemas,
    parse_param_spec,
    semantic_requires_approval,
    semantic_tool_to_openai,
)
from leapflow.skills.tool_executor import ToolBridge, ToolDefinition


def _definition(name: str, parameters: dict[str, str] | None = None) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"test {name}",
        parameters=parameters or {"target": "string (required) — element to act on"},
    )


# ── Parameter spec parsing ─────────────────────────────────────────────


def test_parse_param_spec_required_with_description() -> None:
    spec = parse_param_spec("string (required) — text to type")
    assert spec.type == "string"
    assert spec.required is True
    assert spec.description == "text to type"


def test_parse_param_spec_optional_with_default() -> None:
    spec = parse_param_spec("number (optional, default=30) — max seconds to wait")
    assert spec.type == "number"
    assert spec.required is False
    assert spec.description == "max seconds to wait"


def test_parse_param_spec_no_flags() -> None:
    spec = parse_param_spec("boolean — whether to wait")
    assert spec.type == "boolean"
    assert spec.required is False
    assert spec.description == "whether to wait"


def test_parse_param_spec_no_description() -> None:
    spec = parse_param_spec("int (required)")
    assert spec.type == "integer"
    assert spec.required is True
    assert spec.description == ""


def test_parse_param_spec_empty_and_unknown() -> None:
    assert parse_param_spec("").required is False
    # Unrecognizable head falls back to string with the whole text as description.
    spec = parse_param_spec("??? weird")
    assert spec.type == "string"
    assert spec.description == "??? weird"


# ── Schema conversion ───────────────────────────────────────────────────


def test_semantic_tool_to_openai_mutating_metadata() -> None:
    schema = semantic_tool_to_openai(_definition("click"))
    assert schema is not None
    func = schema["function"]
    assert func["name"] == "click"
    assert func["parameters"]["properties"]["target"]["type"] == "string"
    assert func["parameters"]["required"] == ["target"]
    meta = schema["x_leapflow"]
    assert meta["category"] == DESKTOP_CATEGORY
    assert meta["risk_level"] == "medium"
    assert meta["schema_cost"] == "high"
    assert meta["requires_approval"] is True


def test_semantic_tool_to_openai_observation_metadata() -> None:
    for name in ("observe_ui", "list_apps", "screenshot"):
        schema = semantic_tool_to_openai(_definition(name))
        assert schema is not None
        meta = schema["x_leapflow"]
        assert meta["risk_level"] == "read_only"
        assert meta["requires_approval"] is False
        assert meta["schema_cost"] == "high"


def test_semantic_tool_to_openai_rejects_non_semantic() -> None:
    assert semantic_tool_to_openai(_definition("file_list")) is None
    assert semantic_tool_to_openai(_definition("gp_shell_run")) is None


# ── Bridge-driven collection ────────────────────────────────────────────


class _SemanticBridge:
    """Minimal bridge double exposing tool_definitions() and handlers."""

    def __init__(self, definitions: list[ToolDefinition], handler_names: list[str]) -> None:
        self._definitions = definitions
        self._handlers = {name: object() for name in handler_names}

    def tool_definitions(self) -> list[ToolDefinition]:
        return list(self._definitions)

    @property
    def handlers(self) -> dict[str, object]:
        return dict(self._handlers)


def test_build_semantic_schemas_filters_and_sorts() -> None:
    bridge = _SemanticBridge(
        [
            _definition("click"),
            _definition("file_list"),  # bridge default — excluded
            _definition("observe_ui"),
        ],
        ["click", "observe_ui"],
    )
    schemas = build_semantic_schemas(bridge)
    names = [item["function"]["name"] for item in schemas]
    assert names == ["click", "observe_ui"]  # sorted, non-semantic dropped


def test_build_semantic_schemas_empty_when_offline() -> None:
    assert build_semantic_schemas(None) == []
    # Bridge without any semantic tool (perception offline / MockBridge).
    bridge = _SemanticBridge([_definition("file_list")], ["file_list"])
    assert build_semantic_schemas(bridge) == []


def test_build_semantic_handlers_match_schemas() -> None:
    bridge = _SemanticBridge(
        [_definition("click"), _definition("list_apps"), _definition("shell")],
        ["click", "list_apps", "shell", "file_list"],
    )
    handlers = build_semantic_handlers(bridge)
    assert set(handlers) == {"click", "list_apps"}


def test_build_semantic_handlers_empty_when_offline() -> None:
    assert build_semantic_handlers(None) == {}


def test_real_tool_bridge_handlers_are_exposed() -> None:
    """Lock the ToolBridge.handlers contract the conversion layer relies on."""
    bridge = ToolBridge(object())

    async def _click(params: dict) -> dict:
        return {"ok": True, "clicked": params.get("selector")}

    bridge.register(
        "click", "Click a UI element",
        {"selector": "string (required) — target selector"},
        _click, mutates_state=True,
    )
    assert "click" in bridge.handlers
    assert bridge.handlers["click"] is _click

    schemas = build_semantic_schemas(bridge)
    assert [item["function"]["name"] for item in schemas] == ["click"]
    handlers = build_semantic_handlers(bridge)
    assert set(handlers) == {"click"}


# ── Approval classification ─────────────────────────────────────────────


def test_semantic_requires_approval_split() -> None:
    mutating = {"click", "type_text", "shortcut", "switch_app", "open_url",
                "set_clipboard", "scroll", "select_text", "right_click"}
    passive = SEMANTIC_TOOL_NAMES - mutating
    assert all(semantic_requires_approval(name) for name in mutating)
    assert all(not semantic_requires_approval(name) for name in passive)
    assert not semantic_requires_approval("file_list")


def test_semantic_name_set_is_complete() -> None:
    assert len(SEMANTIC_TOOL_NAMES) == 18
