"""Comprehensive tests for the Plugin Compatibility Assessment Engine (P0).

Tests cover:
- Manifest parsing (LeapFlow and DSH formats)
- Category resolution via taxonomy lookup
- Pipeline end-to-end assessment
- Short-circuit behavior
- Public API contract
"""

from __future__ import annotations

import pytest

from leapflow.learning.compatibility import (
    CompatibilityReport,
    PluginManifestInput,
    Verdict,
    assess_plugin,
)
from leapflow.learning.compatibility.protocol import AdapterSpec, StageResult
from leapflow.learning.compatibility.stages.category_resolver import CategoryResolver
from leapflow.learning.compatibility.stages.manifest_parser import ManifestParser
from leapflow.learning.compatibility.taxonomy import (
    PLUGGABILITY_TAXONOMY,
    TaxonomyEntry,
    resolve_category,
)


# ═══════════════════════════════════════════════════════════════════════
# Stage 1: Manifest Parsing Tests
# ═══════════════════════════════════════════════════════════════════════


class TestManifestParserLeapFlow:
    """Tests for parsing LeapFlow-native manifest dicts."""

    def test_basic_leapflow_manifest(self) -> None:
        """LeapFlow manifest dict is correctly parsed into PluginManifestInput."""
        raw = {
            "name": "my_tool_plugin",
            "version": "1.2.0",
            "entry_point": "my_tool_plugin.main",
            "checksum_sha256": "abc123",
            "metadata": {"category": "tools"},
            "declared_interfaces": ["execute", "describe"],
            "dependencies": ["memory_manager"],
            "permissions": ["fs.read"],
            "execution_model": "async",
        }
        result = ManifestParser.parse_raw(raw)

        assert result.passed is True
        assert result.stage_name == "manifest_parser"
        manifest = result.evidence["manifest"]
        assert isinstance(manifest, PluginManifestInput)
        assert manifest.name == "my_tool_plugin"
        assert manifest.version == "1.2.0"
        assert manifest.category == "tools"
        assert manifest.source_format == "leapflow"
        assert manifest.source_language == "python"
        assert manifest.declared_interfaces == ["execute", "describe"]
        assert manifest.declared_dependencies == ["memory_manager"]
        assert manifest.permissions == ["fs.read"]
        assert manifest.execution_model == "async"

    def test_leapflow_manifest_with_x_leapflow(self) -> None:
        """LeapFlow manifest with x_leapflow metadata section."""
        raw = {
            "name": "signal_plugin",
            "version": "0.5.0",
            "entry_point": "signal.main",
            "x_leapflow": {"category": "signal"},
        }
        result = ManifestParser.parse_raw(raw)

        assert result.passed is True
        manifest = result.evidence["manifest"]
        assert manifest.category == "signal"

    def test_leapflow_manifest_minimal(self) -> None:
        """Minimal LeapFlow manifest with just name and version."""
        raw = {
            "name": "simple_plugin",
            "version": "0.1.0",
            "entry_point": "simple.main",
        }
        result = ManifestParser.parse_raw(raw)

        assert result.passed is True
        manifest = result.evidence["manifest"]
        assert manifest.name == "simple_plugin"
        assert manifest.category == "tools"  # default fallback


class TestManifestParserDSH:
    """Tests for parsing DSH (package.json-like) manifest dicts."""

    def test_basic_dsh_manifest(self) -> None:
        """DSH package.json-like dict is correctly parsed into PluginManifestInput."""
        raw = {
            "name": "@deepseek-ai/dsh-web-search",
            "version": "0.1.0-rc.7",
            "description": "Web search tool for DeepSeek Harness",
            "main": "dist/index.js",
            "keywords": ["web", "search", "tool"],
            "dependencies": {"node-fetch": "^3.0.0"},
            "dsh": {
                "category": "web",
                "interfaces": ["web_search", "web_fetch"],
                "permissions": ["network.outbound"],
                "execution_model": "async",
            },
        }
        result = ManifestParser.parse_raw(raw)

        assert result.passed is True
        manifest = result.evidence["manifest"]
        assert isinstance(manifest, PluginManifestInput)
        assert manifest.name == "@deepseek-ai/dsh-web-search"
        assert manifest.version == "0.1.0-rc.7"
        assert manifest.category == "web"
        assert manifest.source_format == "dsh"
        assert manifest.source_language == "typescript"
        assert manifest.declared_interfaces == ["web_search", "web_fetch"]
        assert manifest.declared_dependencies == ["node-fetch"]
        assert manifest.permissions == ["network.outbound"]

    def test_dsh_manifest_category_from_keywords(self) -> None:
        """DSH manifest extracts category from keywords when no metadata section."""
        raw = {
            "name": "@deepseek-ai/dsh-fs-read",
            "version": "1.0.0",
            "main": "dist/index.js",
            "keywords": ["filesystem", "read"],
        }
        result = ManifestParser.parse_raw(raw)

        assert result.passed is True
        manifest = result.evidence["manifest"]
        assert manifest.category == "filesystem"

    def test_dsh_manifest_category_from_name(self) -> None:
        """DSH manifest infers category from package name when no metadata/keywords."""
        raw = {
            "name": "@deepseek-ai/dsh-shell-exec",
            "version": "0.2.0",
            "main": "dist/index.js",
            "keywords": [],
        }
        result = ManifestParser.parse_raw(raw)

        assert result.passed is True
        manifest = result.evidence["manifest"]
        # Inferred from name: dsh-shell-exec → shell
        assert manifest.category == "shell"

    def test_dsh_manifest_with_leapflow_metadata_section(self) -> None:
        """DSH manifest with 'leapflow' metadata section instead of 'dsh'."""
        raw = {
            "name": "dsh-mcp-bridge",
            "version": "0.3.0",
            "main": "index.js",
            "keywords": ["mcp"],
            "leapflow": {
                "category": "mcp",
                "interfaces": ["connect", "call_tool"],
            },
        }
        result = ManifestParser.parse_raw(raw)

        assert result.passed is True
        manifest = result.evidence["manifest"]
        assert manifest.category == "mcp"
        assert manifest.declared_interfaces == ["connect", "call_tool"]


class TestManifestParserErrors:
    """Tests for manifest parsing error cases."""

    def test_missing_name(self) -> None:
        """Missing name field produces failed StageResult."""
        raw = {"version": "1.0.0", "main": "index.js", "keywords": ["tools"]}
        result = ManifestParser.parse_raw(raw)

        assert result.passed is False
        assert "name" in result.details.lower()

    def test_missing_version(self) -> None:
        """Missing version field produces failed StageResult."""
        raw = {"name": "test-plugin", "main": "index.js", "keywords": ["tools"]}
        result = ManifestParser.parse_raw(raw)

        assert result.passed is False
        assert "version" in result.details.lower()

    def test_non_dict_input(self) -> None:
        """Non-dict input produces failed StageResult."""
        result = ManifestParser.parse_raw("not a dict")  # type: ignore[arg-type]

        assert result.passed is False
        assert "dict" in result.details.lower() or "Expected" in result.details

    def test_empty_dict(self) -> None:
        """Empty dict with no format markers produces failed StageResult."""
        result = ManifestParser.parse_raw({})

        assert result.passed is False


# ═══════════════════════════════════════════════════════════════════════
# Stage 2: Category Resolution Tests
# ═══════════════════════════════════════════════════════════════════════


class TestCategoryResolver:
    """Tests for the category resolution stage."""

    def test_tools_category_compatible(self) -> None:
        """'tools' category resolves to COMPATIBLE with target_protocol=ToolPlugin."""
        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="tools"
        )
        resolver = CategoryResolver()
        result = resolver.assess(manifest, [])

        assert result.passed is True
        assert result.verdict == Verdict.COMPATIBLE
        assert result.evidence["target_protocol"] == "ToolPlugin"

    def test_agent_loop_incompatible(self) -> None:
        """'agent-loop' category resolves to INCOMPATIBLE with reason."""
        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="agent-loop"
        )
        resolver = CategoryResolver()
        result = resolver.assess(manifest, [])

        assert result.passed is False
        assert result.verdict == Verdict.INCOMPATIBLE
        assert "OODA" in result.details or "engine" in result.details

    def test_llm_category_adaptable(self) -> None:
        """'llm' category resolves to ADAPTABLE with target_protocol=LLMProviderPlugin."""
        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="llm"
        )
        resolver = CategoryResolver()
        result = resolver.assess(manifest, [])

        assert result.passed is True
        assert result.verdict == Verdict.ADAPTABLE
        assert result.evidence["target_protocol"] == "LLMProviderPlugin"

    def test_guard_category_partial(self) -> None:
        """'guard' category resolves to PARTIAL."""
        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="guard"
        )
        resolver = CategoryResolver()
        result = resolver.assess(manifest, [])

        assert result.passed is True
        assert result.verdict == Verdict.PARTIAL

    def test_unknown_category_incompatible_fallback(self) -> None:
        """Unknown category falls back to INCOMPATIBLE."""
        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="totally_unknown_category"
        )
        resolver = CategoryResolver()
        result = resolver.assess(manifest, [])

        assert result.passed is False
        assert result.verdict == Verdict.INCOMPATIBLE
        assert "unknown" in result.details.lower() or "Unknown" in result.details


# ═══════════════════════════════════════════════════════════════════════
# Taxonomy Module Tests
# ═══════════════════════════════════════════════════════════════════════


class TestTaxonomy:
    """Tests for the taxonomy module itself."""

    def test_taxonomy_has_25_plus_entries(self) -> None:
        """The taxonomy contains 25+ entries covering all major categories."""
        assert len(PLUGGABILITY_TAXONOMY) >= 25

    def test_resolve_category_known(self) -> None:
        """resolve_category returns correct entry for known categories."""
        entry = resolve_category("tools")
        assert entry.verdict == Verdict.COMPATIBLE
        assert entry.target_protocol == "ToolPlugin"

    def test_resolve_category_unknown(self) -> None:
        """resolve_category returns INCOMPATIBLE fallback for unknown categories."""
        entry = resolve_category("nonexistent_category_xyz")
        assert entry.verdict == Verdict.INCOMPATIBLE
        assert entry.target_protocol is None

    def test_taxonomy_entry_is_namedtuple(self) -> None:
        """TaxonomyEntry is a NamedTuple with correct fields."""
        entry = resolve_category("web")
        assert isinstance(entry, TaxonomyEntry)
        assert hasattr(entry, "target_protocol")
        assert hasattr(entry, "verdict")
        assert hasattr(entry, "reason")


# ═══════════════════════════════════════════════════════════════════════
# Pipeline End-to-End Tests
# ═══════════════════════════════════════════════════════════════════════


class TestPipelineE2E:
    """End-to-end tests for the assess_plugin() pipeline."""

    def test_dsh_tools_plugin_compatible(self) -> None:
        """DSH tools plugin produces CompatibilityReport(final_verdict=COMPATIBLE)."""
        raw = {
            "name": "@deepseek-ai/dsh-web-search",
            "version": "0.1.0-rc.7",
            "main": "dist/index.js",
            "keywords": ["web"],
            "dsh": {"category": "web", "interfaces": ["web_search"]},
        }
        report = assess_plugin(raw)

        assert isinstance(report, CompatibilityReport)
        assert report.final_verdict == Verdict.COMPATIBLE
        assert report.target_protocol == "ToolPlugin"
        assert report.rejection_reason is None
        assert report.manifest.name == "@deepseek-ai/dsh-web-search"
        assert report.is_installable() is True

    def test_dsh_agent_loop_incompatible(self) -> None:
        """DSH agent-loop plugin produces INCOMPATIBLE with rejection reason."""
        raw = {
            "name": "@deepseek-ai/dsh-agent-loop",
            "version": "0.1.0-rc.7",
            "main": "dist/index.js",
            "keywords": ["agent-loop"],
            "dsh": {"category": "agent-loop"},
        }
        report = assess_plugin(raw)

        assert isinstance(report, CompatibilityReport)
        assert report.final_verdict == Verdict.INCOMPATIBLE
        assert report.rejection_reason is not None
        assert len(report.rejection_reason) > 0
        assert report.target_protocol is None
        assert report.is_installable() is False

    def test_dsh_llm_plugin_adaptable(self) -> None:
        """DSH LLM provider plugin produces ADAPTABLE with adapter spec."""
        raw = {
            "name": "@deepseek-ai/dsh-llm-openai",
            "version": "0.2.0",
            "main": "dist/index.js",
            "keywords": ["llm"],
            "dsh": {"category": "llm", "interfaces": ["complete", "stream"]},
        }
        report = assess_plugin(raw)

        assert report.final_verdict == Verdict.ADAPTABLE
        assert report.target_protocol == "LLMProviderPlugin"
        assert report.adapter_spec is not None
        assert report.adapter_spec.target_protocol == "LLMProviderPlugin"
        assert report.adapter_spec.bridge_type == "json_rpc_bridge"
        assert len(report.adaptation_notes) > 0
        assert report.is_installable() is True

    def test_pipeline_short_circuit_on_incompatible(self) -> None:
        """INCOMPATIBLE at stage 2 stops pipeline (only 2 stages recorded)."""
        raw = {
            "name": "@deepseek-ai/dsh-session-persistence",
            "version": "1.0.0",
            "main": "dist/index.js",
            "keywords": ["session"],
            "dsh": {"category": "session"},
        }
        report = assess_plugin(raw)

        assert report.final_verdict == Verdict.INCOMPATIBLE
        # Only 2 stages: manifest_parser and category_resolver
        assert len(report.stages) == 2
        assert report.stages[0].stage_name == "manifest_parser"
        assert report.stages[1].stage_name == "category_resolver"

    def test_leapflow_manifest_compatible(self) -> None:
        """LeapFlow-native manifest for tools category produces COMPATIBLE."""
        raw = {
            "name": "my_file_tool",
            "version": "2.0.0",
            "entry_point": "my_file_tool.main",
            "metadata": {"category": "tools"},
        }
        report = assess_plugin(raw)

        assert report.final_verdict == Verdict.COMPATIBLE
        assert report.manifest.source_format == "leapflow"
        assert report.manifest.category == "tools"

    def test_pre_parsed_manifest_input(self) -> None:
        """Pre-parsed PluginManifestInput works as input."""
        manifest = PluginManifestInput(
            name="pre_parsed_plugin",
            version="1.0.0",
            category="fs",
            source_format="dsh",
        )
        report = assess_plugin(manifest)

        assert report.final_verdict == Verdict.COMPATIBLE
        assert report.manifest.name == "pre_parsed_plugin"

    def test_dsh_tools_category_plugin_compatible(self) -> None:
        """DSH tools-category plugin produces COMPATIBLE verdict."""
        raw = {
            "name": "@deepseek-ai/dsh-tools-fs",
            "version": "0.1.0",
            "main": "dist/index.js",
            "keywords": ["tools"],
            "dsh": {"category": "tools", "interfaces": ["fs_read", "fs_write"]},
        }
        report = assess_plugin(raw)
        assert report.final_verdict == Verdict.COMPATIBLE
        assert report.target_protocol == "ToolPlugin"
        assert report.rejection_reason is None
        assert report.manifest.category == "tools"

    def test_pre_parsed_manifest_with_missing_name_incompatible(self) -> None:
        """Pre-parsed PluginManifestInput with empty name is rejected."""
        bad = PluginManifestInput(
            name="", version="1.0.0", category="tools",
            declared_interfaces=[], declared_dependencies=[],
            config_schema={}, execution_model="async",
            permissions=[], source_language="python",
            raw_manifest={}, source_format="leapflow",
        )
        report = assess_plugin(bad)
        assert report.final_verdict == Verdict.INCOMPATIBLE
        assert "name" in (report.rejection_reason or "").lower()

    def test_invalid_manifest_dict(self) -> None:
        """Invalid manifest dict (no parseable markers) produces INCOMPATIBLE."""
        raw: dict = {}
        report = assess_plugin(raw)

        assert report.final_verdict == Verdict.INCOMPATIBLE
        assert report.rejection_reason is not None


class TestPipelinePublicAPI:
    """Tests for the public API contract."""

    def test_assess_plugin_signature(self) -> None:
        """assess_plugin() accepts dict and returns CompatibilityReport."""
        raw = {
            "name": "test",
            "version": "1.0.0",
            "entry_point": "test.main",
            "metadata": {"category": "tools"},
        }
        result = assess_plugin(raw)
        assert isinstance(result, CompatibilityReport)

    def test_assess_plugin_returns_frozen_report(self) -> None:
        """CompatibilityReport is a frozen dataclass."""
        raw = {
            "name": "test",
            "version": "1.0.0",
            "entry_point": "test.main",
            "metadata": {"category": "tools"},
        }
        report = assess_plugin(raw)

        # Frozen dataclass — mutation raises
        with pytest.raises((AttributeError, TypeError)):
            report.final_verdict = Verdict.INCOMPATIBLE  # type: ignore[misc]

    def test_verdict_enum_values(self) -> None:
        """Verdict enum has all expected members."""
        assert Verdict.COMPATIBLE.value == "compatible"
        assert Verdict.ADAPTABLE.value == "adaptable"
        assert Verdict.PARTIAL.value == "partial"
        assert Verdict.INCOMPATIBLE.value == "incompatible"

    def test_compatibility_report_is_installable(self) -> None:
        """is_installable() returns True for COMPATIBLE/ADAPTABLE/PARTIAL."""
        manifest = PluginManifestInput(name="t", version="1", category="tools")

        compatible = CompatibilityReport(manifest=manifest, final_verdict=Verdict.COMPATIBLE)
        assert compatible.is_installable() is True

        adaptable = CompatibilityReport(manifest=manifest, final_verdict=Verdict.ADAPTABLE)
        assert adaptable.is_installable() is True

        partial = CompatibilityReport(manifest=manifest, final_verdict=Verdict.PARTIAL)
        assert partial.is_installable() is True

        incompatible = CompatibilityReport(manifest=manifest, final_verdict=Verdict.INCOMPATIBLE)
        assert incompatible.is_installable() is False

    def test_stage_result_frozen(self) -> None:
        """StageResult is a frozen dataclass."""
        sr = StageResult(stage_name="test", passed=True)
        with pytest.raises((AttributeError, TypeError)):
            sr.passed = False  # type: ignore[misc]

    def test_adapter_spec_frozen(self) -> None:
        """AdapterSpec is a frozen dataclass."""
        spec = AdapterSpec(
            source_interface="web",
            target_protocol="ToolPlugin",
            bridge_type="json_rpc_bridge",
        )
        with pytest.raises((AttributeError, TypeError)):
            spec.bridge_type = "other"  # type: ignore[misc]
