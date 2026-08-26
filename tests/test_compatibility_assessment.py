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
        """DSH tools plugin produces CompatibilityReport(final_verdict=COMPATIBLE or ADAPTABLE)."""
        raw = {
            "name": "@deepseek-ai/dsh-web-search",
            "version": "0.1.0-rc.7",
            "main": "dist/index.js",
            "keywords": ["web"],
            "dsh": {"category": "web", "interfaces": ["web_search"]},
        }
        report = assess_plugin(raw)

        assert isinstance(report, CompatibilityReport)
        # TypeScript source triggers ADAPTABLE (needs JSON-RPC bridge)
        assert report.final_verdict in (Verdict.COMPATIBLE, Verdict.ADAPTABLE)
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
        """DSH tools-category plugin produces installable verdict."""
        raw = {
            "name": "@deepseek-ai/dsh-tools-fs",
            "version": "0.1.0",
            "main": "dist/index.js",
            "keywords": ["tools"],
            "dsh": {"category": "tools", "interfaces": ["fs_read", "fs_write"]},
        }
        report = assess_plugin(raw)
        # TypeScript triggers ADAPTABLE (bridge needed)
        assert report.final_verdict in (Verdict.COMPATIBLE, Verdict.ADAPTABLE)
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


# ═══════════════════════════════════════════════════════════════════
# P1: Stage 3 — Interface Analyzer Tests
# ═══════════════════════════════════════════════════════════════════


class TestInterfaceAnalyzer:
    """Tests for Stage 3: Interface Analyzer."""

    def test_compatible_interfaces_tool_plugin(self) -> None:
        """Plugin declaring 'execute' matches ToolPlugin requirements."""
        from leapflow.learning.compatibility.stages.interface_analyzer import InterfaceAnalyzer

        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="tools",
            declared_interfaces=["execute", "describe"],
        )
        prior = [StageResult(
            stage_name="category_resolver", passed=True,
            evidence={"target_protocol": "ToolPlugin"},
        )]
        result = InterfaceAnalyzer().assess(manifest, prior)
        assert result.passed is True
        assert result.verdict != Verdict.INCOMPATIBLE
        assert result.evidence["match_type"] in ("exact", "fuzzy")

    def test_compatible_interfaces_llm_plugin(self) -> None:
        """Plugin declaring 'generate' matches LLMProviderPlugin."""
        from leapflow.learning.compatibility.stages.interface_analyzer import InterfaceAnalyzer

        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="llm",
            declared_interfaces=["generate", "stream"],
        )
        prior = [StageResult(
            stage_name="category_resolver", passed=True,
            evidence={"target_protocol": "LLMProviderPlugin"},
        )]
        result = InterfaceAnalyzer().assess(manifest, prior)
        assert result.passed is True
        assert result.evidence["match_type"] == "exact"

    def test_missing_interfaces_incompatible(self) -> None:
        """Plugin declaring completely unrelated interfaces → INCOMPATIBLE."""
        from leapflow.learning.compatibility.stages.interface_analyzer import InterfaceAnalyzer

        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="tools",
            declared_interfaces=["paint_canvas", "render_3d"],
        )
        prior = [StageResult(
            stage_name="category_resolver", passed=True,
            evidence={"target_protocol": "ToolPlugin"},
        )]
        result = InterfaceAnalyzer().assess(manifest, prior)
        assert result.passed is False
        assert result.verdict == Verdict.INCOMPATIBLE

    def test_empty_interfaces_assumed_compatible(self) -> None:
        """Plugin with no declared interfaces assumed compatible (benefit of doubt)."""
        from leapflow.learning.compatibility.stages.interface_analyzer import InterfaceAnalyzer

        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="tools",
            declared_interfaces=[],
        )
        prior = [StageResult(
            stage_name="category_resolver", passed=True,
            evidence={"target_protocol": "ToolPlugin"},
        )]
        result = InterfaceAnalyzer().assess(manifest, prior)
        assert result.passed is True
        assert result.evidence["match_type"] == "assumed"

    def test_fuzzy_match_tool_interface(self) -> None:
        """Fuzzy substring matching catches tool-like interfaces."""
        from leapflow.learning.compatibility.stages.interface_analyzer import InterfaceAnalyzer

        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="tools",
            declared_interfaces=["my_custom_tool_execute"],
        )
        prior = [StageResult(
            stage_name="category_resolver", passed=True,
            evidence={"target_protocol": "ToolPlugin"},
        )]
        result = InterfaceAnalyzer().assess(manifest, prior)
        assert result.passed is True
        assert result.evidence["match_type"] == "fuzzy"


# ═══════════════════════════════════════════════════════════════════
# P1: Stage 4 — Dependency Checker Tests
# ═══════════════════════════════════════════════════════════════════


class TestDependencyChecker:
    """Tests for Stage 4: Dependency Checker."""

    def test_all_satisfiable(self) -> None:
        """All known LeapFlow-satisfiable dependencies pass cleanly."""
        from leapflow.learning.compatibility.stages.dependency_checker import DependencyChecker

        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="tools",
            declared_dependencies=["config", "event_bus", "registry"],
        )
        result = DependencyChecker().assess(manifest, [])
        assert result.passed is True
        assert result.verdict is None or result.verdict == Verdict.COMPATIBLE

    def test_some_shimmable(self) -> None:
        """Shimmable deps produce ADAPTABLE verdict."""
        from leapflow.learning.compatibility.stages.dependency_checker import DependencyChecker

        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="tools",
            declared_dependencies=["config", "cordis", "dsh-logger"],
        )
        result = DependencyChecker().assess(manifest, [])
        assert result.passed is True
        assert result.verdict == Verdict.ADAPTABLE
        assert len(result.evidence["shimmable"]) >= 2

    def test_blocking_deps(self) -> None:
        """Blocking dependencies produce INCOMPATIBLE verdict."""
        from leapflow.learning.compatibility.stages.dependency_checker import DependencyChecker

        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="tools",
            declared_dependencies=["config", "dsh-scope-service"],
        )
        result = DependencyChecker().assess(manifest, [])
        assert result.passed is False
        assert result.verdict == Verdict.INCOMPATIBLE
        assert "dsh-scope-service" in result.evidence["blocking"]

    def test_no_deps(self) -> None:
        """No declared dependencies passes cleanly."""
        from leapflow.learning.compatibility.stages.dependency_checker import DependencyChecker

        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="tools",
            declared_dependencies=[],
        )
        result = DependencyChecker().assess(manifest, [])
        assert result.passed is True

    def test_unknown_deps_satisfiable(self) -> None:
        """Unknown external packages default to satisfiable."""
        from leapflow.learning.compatibility.stages.dependency_checker import DependencyChecker

        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="tools",
            declared_dependencies=["lodash", "moment"],
        )
        result = DependencyChecker().assess(manifest, [])
        assert result.passed is True
        assert result.verdict is None  # All satisfiable


# ═══════════════════════════════════════════════════════════════════
# P1: Stage 5 — Execution Model Tests
# ═══════════════════════════════════════════════════════════════════


class TestExecutionModel:
    """Tests for Stage 5: Execution Model Analyzer."""

    def test_async_python_compatible(self) -> None:
        """Async Python plugin is natively compatible."""
        from leapflow.learning.compatibility.stages.execution_model import ExecutionModelAnalyzer

        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="tools",
            execution_model="async", source_language="python",
        )
        result = ExecutionModelAnalyzer().assess(manifest, [])
        assert result.passed is True
        assert result.verdict is None  # Fully compatible
        assert result.evidence["requires_bridge"] is False

    def test_sync_python_compatible(self) -> None:
        """Sync Python plugin is compatible (wrapped in executor)."""
        from leapflow.learning.compatibility.stages.execution_model import ExecutionModelAnalyzer

        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="tools",
            execution_model="sync", source_language="python",
        )
        result = ExecutionModelAnalyzer().assess(manifest, [])
        assert result.passed is True
        assert result.verdict is None

    def test_worker_model_adaptable(self) -> None:
        """Worker execution model maps to subprocess (ADAPTABLE)."""
        from leapflow.learning.compatibility.stages.execution_model import ExecutionModelAnalyzer

        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="tools",
            execution_model="worker", source_language="python",
        )
        result = ExecutionModelAnalyzer().assess(manifest, [])
        assert result.passed is True
        assert result.verdict == Verdict.ADAPTABLE

    def test_streaming_model_adaptable(self) -> None:
        """Streaming model maps to async generator (ADAPTABLE)."""
        from leapflow.learning.compatibility.stages.execution_model import ExecutionModelAnalyzer

        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="tools",
            execution_model="streaming", source_language="python",
        )
        result = ExecutionModelAnalyzer().assess(manifest, [])
        assert result.passed is True
        assert result.verdict == Verdict.ADAPTABLE

    def test_typescript_requires_bridge(self) -> None:
        """TypeScript source requires JSON-RPC bridge (ADAPTABLE)."""
        from leapflow.learning.compatibility.stages.execution_model import ExecutionModelAnalyzer

        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="tools",
            execution_model="async", source_language="typescript",
        )
        result = ExecutionModelAnalyzer().assess(manifest, [])
        assert result.passed is True
        assert result.verdict == Verdict.ADAPTABLE
        assert result.evidence["requires_bridge"] is True

    def test_unknown_language_partial(self) -> None:
        """Unknown source language produces PARTIAL verdict."""
        from leapflow.learning.compatibility.stages.execution_model import ExecutionModelAnalyzer

        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="tools",
            execution_model="async", source_language="elixir",
        )
        result = ExecutionModelAnalyzer().assess(manifest, [])
        assert result.passed is True
        assert result.verdict == Verdict.PARTIAL


# ═══════════════════════════════════════════════════════════════════
# P1: Stage 6 — Security Classifier Tests
# ═══════════════════════════════════════════════════════════════════


class TestSecurityClassifier:
    """Tests for Stage 6: Security Classifier."""

    def test_no_permissions_low_risk(self) -> None:
        """No permissions declared → LOW risk."""
        from leapflow.learning.compatibility.stages.security_classifier import SecurityClassifier

        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="tools",
            permissions=[],
        )
        result = SecurityClassifier().assess(manifest, [])
        assert result.passed is True
        assert result.verdict is None
        assert result.evidence["risk_level"] == "low"

    def test_read_only_low_risk(self) -> None:
        """Read-only permissions → LOW risk."""
        from leapflow.learning.compatibility.stages.security_classifier import SecurityClassifier

        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="tools",
            permissions=["fs.read", "config.read"],
        )
        result = SecurityClassifier().assess(manifest, [])
        assert result.passed is True
        assert result.evidence["risk_level"] == "low"

    def test_network_medium_risk(self) -> None:
        """Network outbound → MEDIUM risk."""
        from leapflow.learning.compatibility.stages.security_classifier import SecurityClassifier

        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="tools",
            permissions=["network.outbound"],
        )
        result = SecurityClassifier().assess(manifest, [])
        assert result.passed is True
        assert result.evidence["risk_level"] == "medium"

    def test_shell_high_risk_sandbox(self) -> None:
        """Shell execute → HIGH risk, recommend sandbox."""
        from leapflow.learning.compatibility.stages.security_classifier import SecurityClassifier

        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="tools",
            permissions=["shell.execute"],
            source_format="leapflow",
        )
        result = SecurityClassifier().assess(manifest, [])
        assert result.passed is True
        assert result.verdict == Verdict.ADAPTABLE
        assert result.evidence["risk_level"] == "high"
        assert result.evidence.get("recommendation") == "sandbox"

    def test_critical_untrusted_rejected(self) -> None:
        """CRITICAL permissions from untrusted DSH source → INCOMPATIBLE."""
        from leapflow.learning.compatibility.stages.security_classifier import SecurityClassifier

        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="tools",
            permissions=["credential_access", "system_modify"],
            source_format="dsh",
        )
        result = SecurityClassifier().assess(manifest, [])
        assert result.passed is False
        assert result.verdict == Verdict.INCOMPATIBLE
        assert "reject" in result.evidence.get("recommendation", "")

    def test_critical_trusted_sandbox(self) -> None:
        """CRITICAL permissions from trusted source → sandbox recommendation."""
        from leapflow.learning.compatibility.stages.security_classifier import SecurityClassifier

        manifest = PluginManifestInput(
            name="test", version="1.0.0", category="tools",
            permissions=["credential_access"],
            source_format="leapflow",
        )
        result = SecurityClassifier().assess(manifest, [])
        assert result.passed is True
        assert result.verdict == Verdict.ADAPTABLE
        assert result.evidence["risk_level"] == "critical"


# ═══════════════════════════════════════════════════════════════════
# P1: Verdict Synthesizer Tests
# ═══════════════════════════════════════════════════════════════════


class TestVerdictSynthesizer:
    """Tests for the verdict synthesizer."""

    def test_all_pass_compatible(self) -> None:
        """All stages pass without adaptation → COMPATIBLE."""
        from leapflow.learning.compatibility.verdict import synthesize_verdict

        manifest = PluginManifestInput(name="t", version="1", category="tools")
        stages = [
            StageResult(stage_name="manifest_parser", passed=True),
            StageResult(stage_name="category_resolver", passed=True, verdict=Verdict.COMPATIBLE,
                        evidence={"target_protocol": "ToolPlugin"}),
            StageResult(stage_name="interface_analyzer", passed=True),
            StageResult(stage_name="dependency_checker", passed=True),
            StageResult(stage_name="execution_model_analyzer", passed=True),
            StageResult(stage_name="security_classifier", passed=True),
        ]
        report = synthesize_verdict(manifest, stages)
        assert report.final_verdict == Verdict.COMPATIBLE
        assert report.target_protocol == "ToolPlugin"

    def test_one_adaptable(self) -> None:
        """One stage ADAPTABLE → final ADAPTABLE."""
        from leapflow.learning.compatibility.verdict import synthesize_verdict

        manifest = PluginManifestInput(name="t", version="1", category="llm", source_language="typescript")
        stages = [
            StageResult(stage_name="manifest_parser", passed=True),
            StageResult(stage_name="category_resolver", passed=True, verdict=Verdict.ADAPTABLE,
                        evidence={"target_protocol": "LLMProviderPlugin"}, details="needs adapter"),
            StageResult(stage_name="interface_analyzer", passed=True),
            StageResult(stage_name="dependency_checker", passed=True),
            StageResult(stage_name="execution_model_analyzer", passed=True, verdict=Verdict.ADAPTABLE,
                        details="requires bridge"),
            StageResult(stage_name="security_classifier", passed=True),
        ]
        report = synthesize_verdict(manifest, stages)
        assert report.final_verdict == Verdict.ADAPTABLE
        assert report.adapter_spec is not None
        assert len(report.adaptation_notes) >= 1

    def test_one_incompatible(self) -> None:
        """One stage INCOMPATIBLE → final INCOMPATIBLE."""
        from leapflow.learning.compatibility.verdict import synthesize_verdict

        manifest = PluginManifestInput(name="t", version="1", category="tools")
        stages = [
            StageResult(stage_name="manifest_parser", passed=True),
            StageResult(stage_name="category_resolver", passed=True, evidence={"target_protocol": "ToolPlugin"}),
            StageResult(stage_name="interface_analyzer", passed=False, verdict=Verdict.INCOMPATIBLE,
                        details="No matching interfaces"),
            StageResult(stage_name="dependency_checker", passed=True),
        ]
        report = synthesize_verdict(manifest, stages)
        assert report.final_verdict == Verdict.INCOMPATIBLE
        assert report.rejection_reason == "No matching interfaces"

    def test_partial_verdict(self) -> None:
        """Stage with PARTIAL → final PARTIAL."""
        from leapflow.learning.compatibility.verdict import synthesize_verdict

        manifest = PluginManifestInput(name="t", version="1", category="guard")
        stages = [
            StageResult(stage_name="manifest_parser", passed=True),
            StageResult(stage_name="category_resolver", passed=True, verdict=Verdict.PARTIAL,
                        evidence={"target_protocol": "ToolPlugin"}, details="subset usable"),
            StageResult(stage_name="interface_analyzer", passed=True),
            StageResult(stage_name="dependency_checker", passed=True),
            StageResult(stage_name="execution_model_analyzer", passed=True),
            StageResult(stage_name="security_classifier", passed=True),
        ]
        report = synthesize_verdict(manifest, stages)
        assert report.final_verdict == Verdict.PARTIAL

    def test_mixed_adaptable_partial_yields_adaptable(self) -> None:
        """ADAPTABLE takes precedence over PARTIAL."""
        from leapflow.learning.compatibility.verdict import synthesize_verdict

        manifest = PluginManifestInput(name="t", version="1", category="tools", source_language="typescript")
        stages = [
            StageResult(stage_name="manifest_parser", passed=True),
            StageResult(stage_name="category_resolver", passed=True,
                        evidence={"target_protocol": "ToolPlugin"}),
            StageResult(stage_name="interface_analyzer", passed=True, verdict=Verdict.ADAPTABLE,
                        details="fuzzy match"),
            StageResult(stage_name="dependency_checker", passed=True),
            StageResult(stage_name="execution_model_analyzer", passed=True, verdict=Verdict.PARTIAL,
                        details="unknown model"),
            StageResult(stage_name="security_classifier", passed=True),
        ]
        report = synthesize_verdict(manifest, stages)
        assert report.final_verdict == Verdict.ADAPTABLE


# ═══════════════════════════════════════════════════════════════════
# P1: Full Pipeline Tests (6 Stages)
# ═══════════════════════════════════════════════════════════════════


class TestFullPipelineP1:
    """Full pipeline tests exercising all 6 stages."""

    def test_dsh_web_tool_all_6_stages(self) -> None:
        """DSH web tool passes all 6 stages → COMPATIBLE."""
        raw = {
            "name": "@deepseek-ai/dsh-web-search",
            "version": "0.1.0",
            "main": "dist/index.js",
            "keywords": ["web"],
            "dsh": {
                "category": "web",
                "interfaces": ["web_search", "web_fetch"],
                "permissions": ["network.outbound"],
                "execution_model": "async",
            },
            "dependencies": {"node-fetch": "^3.0.0"},
        }
        report = assess_plugin(raw)
        # Web tool with network perm and typescript: should be ADAPTABLE
        # (typescript bridge + medium risk is acceptable)
        assert report.final_verdict in (Verdict.COMPATIBLE, Verdict.ADAPTABLE)
        assert report.is_installable() is True
        assert len(report.stages) == 6

    def test_dsh_llm_plugin_all_stages(self) -> None:
        """DSH LLM plugin runs all 6 stages and produces ADAPTABLE."""
        raw = {
            "name": "@deepseek-ai/dsh-llm-openai",
            "version": "0.2.0",
            "main": "dist/index.js",
            "keywords": ["llm"],
            "dsh": {
                "category": "llm",
                "interfaces": ["complete", "stream"],
                "permissions": ["network.outbound"],
                "execution_model": "async",
            },
            "dependencies": {"node-fetch": "^3.0.0"},
        }
        report = assess_plugin(raw)
        assert report.final_verdict == Verdict.ADAPTABLE
        assert report.target_protocol == "LLMProviderPlugin"
        assert report.adapter_spec is not None
        assert len(report.stages) == 6

    def test_blocking_deps_short_circuits_at_stage4(self) -> None:
        """Plugin with blocking deps short-circuits at stage 4."""
        manifest = PluginManifestInput(
            name="blocked", version="1.0.0", category="tools",
            declared_interfaces=["execute"],
            declared_dependencies=["dsh-scope-service"],
            source_language="python", execution_model="async",
        )
        report = assess_plugin(manifest)
        assert report.final_verdict == Verdict.INCOMPATIBLE
        # Should have stages 1-4 (short-circuit at 4)
        assert len(report.stages) == 4
        assert report.stages[3].stage_name == "dependency_checker"

    def test_incompatible_interfaces_short_circuits_at_stage3(self) -> None:
        """Plugin with completely wrong interfaces stops at stage 3."""
        manifest = PluginManifestInput(
            name="wrong_iface", version="1.0.0", category="tools",
            declared_interfaces=["paint_canvas", "render_3d"],
            source_language="python", execution_model="async",
        )
        report = assess_plugin(manifest)
        assert report.final_verdict == Verdict.INCOMPATIBLE
        assert len(report.stages) == 3
        assert report.stages[2].stage_name == "interface_analyzer"

    def test_critical_perms_untrusted_rejected_at_stage6(self) -> None:
        """DSH plugin with critical permissions rejected at stage 6."""
        raw = {
            "name": "@malicious/dsh-rootkit",
            "version": "0.0.1",
            "main": "dist/index.js",
            "keywords": ["tools"],
            "dsh": {
                "category": "tools",
                "interfaces": ["execute"],
                "permissions": ["credential_access", "system_modify"],
                "execution_model": "async",
            },
        }
        report = assess_plugin(raw)
        assert report.final_verdict == Verdict.INCOMPATIBLE
        assert len(report.stages) == 6
        assert report.stages[5].stage_name == "security_classifier"

    def test_python_async_tool_fully_compatible(self) -> None:
        """Native Python async tool passes all stages cleanly."""
        manifest = PluginManifestInput(
            name="my_native_tool", version="2.0.0", category="tools",
            declared_interfaces=["execute", "describe"],
            declared_dependencies=["config", "event_bus"],
            permissions=["fs.read"],
            execution_model="async", source_language="python",
            source_format="leapflow",
        )
        report = assess_plugin(manifest)
        assert report.final_verdict == Verdict.COMPATIBLE
        assert report.is_installable() is True
        assert len(report.stages) == 6
        assert report.adapter_spec is None

    def test_typescript_worker_tool_adaptable(self) -> None:
        """TypeScript worker tool needs bridge and model adaptation."""
        raw = {
            "name": "dsh-code-runner",
            "version": "1.0.0",
            "main": "dist/index.js",
            "keywords": ["code-runtime"],
            "dsh": {
                "category": "code-runtime",
                "interfaces": ["run", "exec_code"],
                "permissions": ["shell.execute"],
                "execution_model": "worker",
            },
            "dependencies": {"dsh-sdk": "^1.0.0"},
        }
        report = assess_plugin(raw)
        assert report.final_verdict == Verdict.ADAPTABLE
        assert report.is_installable() is True
        assert report.adapter_spec is not None
        assert len(report.stages) == 6


# ═══════════════════════════════════════════════════════════════════
# P1: assess_compatibility Tool Tests
# ═══════════════════════════════════════════════════════════════════


class TestAssessCompatibilityTool:
    """Tests for the assess_compatibility tool handler."""

    @pytest.mark.asyncio
    async def test_assess_tool_returns_report(self) -> None:
        """assess_compatibility returns structured report for valid manifest."""
        from leapflow.plugins.tool_plugins.self_management import SelfManagementPlugin

        plugin = SelfManagementPlugin()
        manifest = {
            "name": "test_plugin",
            "version": "1.0.0",
            "entry_point": "test.main",
            "metadata": {"category": "tools"},
            "declared_interfaces": ["execute"],
        }
        result = await plugin._assess_compatibility_handler(manifest=manifest)
        assert result["ok"] is True
        assert result["final_verdict"] in ("compatible", "adaptable", "partial", "incompatible")
        assert result["is_installable"] is True
        assert "stages" in result
        assert len(result["stages"]) == 6

    @pytest.mark.asyncio
    async def test_assess_tool_incompatible_manifest(self) -> None:
        """assess_compatibility returns INCOMPATIBLE for agent-loop category."""
        from leapflow.plugins.tool_plugins.self_management import SelfManagementPlugin

        plugin = SelfManagementPlugin()
        manifest = {
            "name": "@dsh/agent-loop",
            "version": "1.0.0",
            "main": "dist/index.js",
            "keywords": ["agent-loop"],
            "dsh": {"category": "agent-loop"},
        }
        result = await plugin._assess_compatibility_handler(manifest=manifest)
        assert result["ok"] is True
        assert result["final_verdict"] == "incompatible"
        assert result["is_installable"] is False
        assert result["rejection_reason"] is not None

    @pytest.mark.asyncio
    async def test_assess_tool_missing_manifest(self) -> None:
        """assess_compatibility returns error when manifest is missing."""
        from leapflow.plugins.tool_plugins.self_management import SelfManagementPlugin

        plugin = SelfManagementPlugin()
        result = await plugin._assess_compatibility_handler()
        assert result["ok"] is False
        assert "manifest" in result["error"].lower()

    def test_assess_tool_is_registered(self) -> None:
        """assess_compatibility tool is present in the plugin's tools list."""
        from leapflow.plugins.tool_plugins.self_management import SelfManagementPlugin

        plugin = SelfManagementPlugin()
        tool_names = [t.name for t in plugin.tools]
        assert "assess_compatibility" in tool_names
        # Verify metadata
        tool = next(t for t in plugin.tools if t.name == "assess_compatibility")
        assert tool.mutates_state is False
        assert tool.x_leapflow["risk_level"] == "none"


# ═══════════════════════════════════════════════════════════════════
# P1: Install Gate Tests
# ═══════════════════════════════════════════════════════════════════


class TestInstallGate:
    """Tests for the compatibility install gate."""

    @pytest.mark.asyncio
    async def test_marketplace_incompatible_blocked(self) -> None:
        """Marketplace install with INCOMPATIBLE manifest is blocked."""
        from unittest.mock import AsyncMock, MagicMock

        from leapflow.plugins.tool_plugins.self_management import SelfManagementPlugin

        plugin = SelfManagementPlugin()
        # Mock approval gate
        plugin._plugin_approval_gate = MagicMock()
        plugin._plugin_approval_gate.check = AsyncMock(return_value=(True, None))

        # Mock marketplace client that returns an incompatible manifest
        mock_client = MagicMock()
        mock_client.resolve_manifest = MagicMock(return_value={
            "name": "@dsh/agent-loop",
            "version": "1.0.0",
            "main": "dist/index.js",
            "keywords": ["agent-loop"],
            "dsh": {"category": "agent-loop"},
        })
        plugin._marketplace_client = mock_client

        result = await plugin._install_from_marketplace_with_gate("test_plugin", "agent-loop-pkg")
        assert result["ok"] is False
        assert "INCOMPATIBLE" in result["error"]
        assert result.get("verdict") == "incompatible"

    @pytest.mark.asyncio
    async def test_marketplace_compatible_proceeds(self) -> None:
        """Marketplace install with compatible manifest proceeds to install."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from leapflow.plugins.tool_plugins.self_management import SelfManagementPlugin

        plugin = SelfManagementPlugin()

        # Mock marketplace client with compatible manifest
        mock_client = MagicMock()
        mock_client.resolve_manifest = MagicMock(return_value={
            "name": "my-tool",
            "version": "1.0.0",
            "entry_point": "my_tool.main",
            "metadata": {"category": "tools"},
        })
        mock_client.install = MagicMock(return_value={
            "ok": True,
            "installed_path": "/tmp/test_plugin.py",
        })
        plugin._marketplace_client = mock_client

        # Mock the actual install to avoid file system operations
        with patch.object(plugin, "_install_from_marketplace", new_callable=AsyncMock) as mock_install:
            mock_install.return_value = {"ok": True, "action": "install"}
            result = await plugin._install_from_marketplace_with_gate("test_plugin", "my-tool-pkg")

        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_marketplace_no_manifest_still_proceeds(self) -> None:
        """If resolve_manifest fails, gate degrades gracefully and proceeds."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from leapflow.plugins.tool_plugins.self_management import SelfManagementPlugin

        plugin = SelfManagementPlugin()

        # Mock marketplace client where resolve_manifest raises
        mock_client = MagicMock()
        mock_client.resolve_manifest = MagicMock(side_effect=RuntimeError("not found"))
        plugin._marketplace_client = mock_client

        with patch.object(plugin, "_install_from_marketplace", new_callable=AsyncMock) as mock_install:
            mock_install.return_value = {"ok": True, "action": "install"}
            result = await plugin._install_from_marketplace_with_gate("test_plugin", "some-pkg")

        # Should proceed to install despite gate failure
        assert result["ok"] is True

# ═══════════════════════════════════════════════════════════════════
# P2: File-Path Manifest Loading Tests
# ═══════════════════════════════════════════════════════════════════


class TestFilePathManifestLoading:
    """Tests for assess_plugin() accepting a file path (str or Path)."""

    def test_load_from_json_path_string(self, tmp_path) -> None:
        """A path-like string ending in .json is read and assessed."""
        import json

        manifest_file = tmp_path / "plugin.json"
        manifest_file.write_text(
            json.dumps(
                {
                    "name": "my_file_tool",
                    "version": "1.0.0",
                    "entry_point": "my_file_tool.main",
                    "metadata": {"category": "tools"},
                }
            ),
            encoding="utf-8",
        )
        report = assess_plugin(str(manifest_file))

        assert isinstance(report, CompatibilityReport)
        assert report.final_verdict == Verdict.COMPATIBLE
        assert report.manifest.name == "my_file_tool"

    def test_load_from_path_object(self, tmp_path) -> None:
        """A pathlib.Path object is read and assessed."""
        import json
        from pathlib import Path

        manifest_file = tmp_path / "dsh_plugin.json"
        manifest_file.write_text(
            json.dumps(
                {
                    "name": "@deepseek-ai/dsh-web-search",
                    "version": "0.1.0",
                    "main": "dist/index.js",
                    "keywords": ["web"],
                    "dsh": {"category": "web", "interfaces": ["web_search"]},
                }
            ),
            encoding="utf-8",
        )
        assert isinstance(manifest_file, Path)
        report = assess_plugin(manifest_file)

        assert report.is_installable() is True
        assert report.manifest.name == "@deepseek-ai/dsh-web-search"

    def test_nonexistent_file_incompatible(self, tmp_path) -> None:
        """A missing file path resolves to INCOMPATIBLE with a clear reason."""
        missing = tmp_path / "does_not_exist.json"
        report = assess_plugin(str(missing))

        assert report.final_verdict == Verdict.INCOMPATIBLE
        assert report.rejection_reason is not None
        assert "not found" in report.rejection_reason.lower()

    def test_nonexistent_path_object_incompatible(self, tmp_path) -> None:
        """A missing Path object resolves to INCOMPATIBLE."""
        missing = tmp_path / "nope.json"
        report = assess_plugin(missing)

        assert report.final_verdict == Verdict.INCOMPATIBLE
        assert report.is_installable() is False

    def test_invalid_json_incompatible(self, tmp_path) -> None:
        """A file with invalid JSON resolves to INCOMPATIBLE with details."""
        bad = tmp_path / "broken.json"
        bad.write_text("{ this is : not valid json ,", encoding="utf-8")
        report = assess_plugin(str(bad))

        assert report.final_verdict == Verdict.INCOMPATIBLE
        assert report.rejection_reason is not None
        assert "json" in report.rejection_reason.lower()

    def test_json_file_not_object_incompatible(self, tmp_path) -> None:
        """A JSON file containing a non-object (list) is INCOMPATIBLE."""
        arr = tmp_path / "arr.json"
        arr.write_text("[1, 2, 3]", encoding="utf-8")
        report = assess_plugin(str(arr))

        assert report.final_verdict == Verdict.INCOMPATIBLE
        assert "object" in (report.rejection_reason or "").lower()

    def test_non_path_string_unsupported(self) -> None:
        """A string that is not path-like is an unsupported manifest format."""
        report = assess_plugin("just some random text")

        assert report.final_verdict == Verdict.INCOMPATIBLE
        assert "unsupported manifest format" in (report.rejection_reason or "").lower()

    def test_relative_dot_path_recognized(self, tmp_path, monkeypatch) -> None:
        """A './'-prefixed relative path is recognized as a file path."""
        import json

        manifest_file = tmp_path / "rel.json"
        manifest_file.write_text(
            json.dumps(
                {
                    "name": "rel_tool",
                    "version": "1.0.0",
                    "entry_point": "rel_tool.main",
                    "metadata": {"category": "tools"},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        report = assess_plugin("./rel.json")

        assert report.final_verdict == Verdict.COMPATIBLE
        assert report.manifest.name == "rel_tool"

    def test_dict_input_still_supported(self) -> None:
        """Normalizing file paths does not regress plain dict input."""
        report = assess_plugin(
            {
                "name": "dict_tool",
                "version": "1.0.0",
                "entry_point": "dict_tool.main",
                "metadata": {"category": "tools"},
            }
        )
        assert report.final_verdict == Verdict.COMPATIBLE


# ═══════════════════════════════════════════════════════════════════
# P2: DSH → LeapFlow Manifest Converter Tests
# ═══════════════════════════════════════════════════════════════════


class TestManifestConverter:
    """Tests for convert_dsh_to_leapflow()."""

    def test_full_mapping(self) -> None:
        """Full DSH manifest maps every field correctly."""
        from leapflow.learning.compatibility.manifest_converter import (
            convert_dsh_to_leapflow,
        )

        dsh = {
            "name": "@deepseek-ai/dsh-web-search",
            "version": "0.1.0-rc.7",
            "main": "dist/index.js",
            "description": "Web search for DSH",
            "keywords": ["web"],
            "dependencies": {"node-fetch": "^3.0.0"},
            "dsh": {"category": "web"},
        }
        result = convert_dsh_to_leapflow(dsh)

        assert result["name"] == "web_search"
        assert result["version"] == "0.1.0-rc.7"
        assert result["entry_point"] == "dist/index.js"
        assert result["description"] == "Web search for DSH"
        assert result["requires_sandbox"] is True
        assert result["checksum_sha256"] is None
        assert result["plugin_type"] == "tool"
        assert result["dependencies"] == ["node-fetch"]
        assert result["x_dsh_category"] == "web"
        assert result["x_dsh_original"] == dsh

    def test_name_prefix_stripping(self) -> None:
        """Org scope and dsh- prefix are stripped; hyphens become underscores."""
        from leapflow.learning.compatibility.manifest_converter import (
            convert_dsh_to_leapflow,
        )

        result = convert_dsh_to_leapflow(
            {"name": "@org/dsh-shell-exec", "version": "1.0.0"}
        )
        assert result["name"] == "shell_exec"

    def test_name_without_prefix(self) -> None:
        """A plain hyphenated name just gets hyphens converted."""
        from leapflow.learning.compatibility.manifest_converter import (
            convert_dsh_to_leapflow,
        )

        result = convert_dsh_to_leapflow({"name": "code-runner", "version": "1.0.0"})
        assert result["name"] == "code_runner"

    def test_empty_name_fallback(self) -> None:
        """Missing name falls back to a placeholder."""
        from leapflow.learning.compatibility.manifest_converter import (
            convert_dsh_to_leapflow,
        )

        result = convert_dsh_to_leapflow({"version": "1.0.0"})
        assert result["name"] == "unknown_plugin"

    def test_no_description_defaults_empty(self) -> None:
        """A DSH manifest with no description yields an empty string."""
        from leapflow.learning.compatibility.manifest_converter import (
            convert_dsh_to_leapflow,
        )

        result = convert_dsh_to_leapflow(
            {"name": "dsh-fs", "version": "1.0.0", "main": "index.js"}
        )
        assert result["description"] == ""

    def test_no_dsh_section_category_from_keywords(self) -> None:
        """With no dsh section, category is inferred from keywords."""
        from leapflow.learning.compatibility.manifest_converter import (
            convert_dsh_to_leapflow,
        )

        result = convert_dsh_to_leapflow(
            {
                "name": "dsh-fs-read",
                "version": "1.0.0",
                "keywords": ["filesystem", "read"],
            }
        )
        assert result["x_dsh_category"] == "filesystem"

    def test_no_dsh_no_keywords_category_empty(self) -> None:
        """No dsh section and no keywords → empty category."""
        from leapflow.learning.compatibility.manifest_converter import (
            convert_dsh_to_leapflow,
        )

        result = convert_dsh_to_leapflow({"name": "dsh-x", "version": "1.0.0"})
        assert result["x_dsh_category"] == ""

    def test_missing_version_defaults(self) -> None:
        """Missing version defaults to 0.0.0."""
        from leapflow.learning.compatibility.manifest_converter import (
            convert_dsh_to_leapflow,
        )

        result = convert_dsh_to_leapflow({"name": "dsh-x", "main": "index.js"})
        assert result["version"] == "0.0.0"

    def test_dependencies_as_list(self) -> None:
        """A list-form dependencies field is preserved (string items only)."""
        from leapflow.learning.compatibility.manifest_converter import (
            convert_dsh_to_leapflow,
        )

        result = convert_dsh_to_leapflow(
            {"name": "dsh-x", "version": "1.0.0", "dependencies": ["a", "b", 3]}
        )
        assert result["dependencies"] == ["a", "b"]

    def test_original_is_copied_not_referenced(self) -> None:
        """x_dsh_original is a copy so mutating the source does not leak in."""
        from leapflow.learning.compatibility.manifest_converter import (
            convert_dsh_to_leapflow,
        )

        dsh = {"name": "dsh-x", "version": "1.0.0"}
        result = convert_dsh_to_leapflow(dsh)
        dsh["name"] = "mutated"
        assert result["x_dsh_original"]["name"] == "dsh-x"


# ═══════════════════════════════════════════════════════════════════
# P2: Adapter Generator Tests
# ═══════════════════════════════════════════════════════════════════


def _sample_adapter_inputs():
    """Build a representative (AdapterSpec, PluginManifestInput) pair."""
    spec = AdapterSpec(
        source_interface="web",
        target_protocol="ToolPlugin",
        bridge_type="json_rpc_bridge",
        shim_methods=["config"],
        estimated_complexity="low",
    )
    manifest = PluginManifestInput(
        name="@deepseek-ai/dsh-web-search",
        version="0.1.0",
        category="web",
        declared_interfaces=["web_search", "web_fetch"],
        source_language="typescript",
        raw_manifest={"main": "dist/index.js"},
        source_format="dsh",
    )
    return spec, manifest


class TestAdapterGeneratorTemplate:
    """Tests for generate_adapter_template() (no LLM)."""

    def test_template_produces_valid_python(self, tmp_path) -> None:
        """The generated template is syntactically valid Python (py_compile)."""
        import py_compile

        from leapflow.learning.compatibility.adapter_generator import (
            generate_adapter_template,
        )

        spec, manifest = _sample_adapter_inputs()
        code = generate_adapter_template(spec, manifest)

        out = tmp_path / "generated_adapter.py"
        out.write_text(code, encoding="utf-8")
        # Raises PyCompileError if the output is not valid Python.
        py_compile.compile(str(out), doraise=True)

    def test_template_compiles_with_compile_builtin(self) -> None:
        """The generated template compiles via the compile() builtin."""
        from leapflow.learning.compatibility.adapter_generator import (
            generate_adapter_template,
        )

        spec, manifest = _sample_adapter_inputs()
        code = generate_adapter_template(spec, manifest)
        # Should not raise
        compile(code, "<generated>", "exec")

    def test_template_class_name_and_plugin_id(self) -> None:
        """Output contains the correct class name and plugin_id."""
        from leapflow.learning.compatibility.adapter_generator import (
            generate_adapter_template,
        )

        spec, manifest = _sample_adapter_inputs()
        code = generate_adapter_template(spec, manifest)

        assert "class DshWebSearchBridgePlugin:" in code
        assert 'return "dsh_web_search_bridge"' in code

    def test_template_declares_handler_per_interface(self) -> None:
        """Each declared interface has a ToolMetadata entry and handler."""
        from leapflow.learning.compatibility.adapter_generator import (
            generate_adapter_template,
        )

        spec, manifest = _sample_adapter_inputs()
        code = generate_adapter_template(spec, manifest)

        assert 'name="web_search"' in code
        assert 'name="web_fetch"' in code
        assert "async def _handle_web_search(" in code
        assert "async def _handle_web_fetch(" in code

    def test_template_notes_auto_generated_and_source(self) -> None:
        """Docstring notes it is auto-generated and names the wrapped plugin."""
        from leapflow.learning.compatibility.adapter_generator import (
            generate_adapter_template,
        )

        spec, manifest = _sample_adapter_inputs()
        code = generate_adapter_template(spec, manifest)

        assert "auto-generated" in code.lower()
        assert "@deepseek-ai/dsh-web-search" in code

    def test_template_uses_sandbox_host_bridge(self) -> None:
        """Handlers delegate to a SandboxHost subprocess bridge."""
        from leapflow.learning.compatibility.adapter_generator import (
            generate_adapter_template,
        )

        spec, manifest = _sample_adapter_inputs()
        code = generate_adapter_template(spec, manifest)

        assert "from leapflow.plugins.sandbox.sandbox_host import SandboxHost" in code
        assert "self._invoke_bridge(" in code

    def test_template_no_interfaces_falls_back_to_invoke(self) -> None:
        """With no declared interfaces, a single 'invoke' tool is generated."""
        from leapflow.learning.compatibility.adapter_generator import (
            generate_adapter_template,
        )

        spec = AdapterSpec(
            source_interface="tools",
            target_protocol="ToolPlugin",
            bridge_type="json_rpc_bridge",
        )
        manifest = PluginManifestInput(
            name="dsh-empty",
            version="1.0.0",
            category="tools",
            declared_interfaces=[],
            source_language="typescript",
        )
        code = generate_adapter_template(spec, manifest)
        compile(code, "<generated>", "exec")
        assert 'name="invoke"' in code
        assert "async def _handle_invoke(" in code

    def test_template_sanitizes_interface_names(self) -> None:
        """Interface names with dots/dashes become valid handler identifiers."""
        from leapflow.learning.compatibility.adapter_generator import (
            generate_adapter_template,
        )

        spec = AdapterSpec(
            source_interface="tools",
            target_protocol="ToolPlugin",
            bridge_type="json_rpc_bridge",
        )
        manifest = PluginManifestInput(
            name="dsh-weird",
            version="1.0.0",
            category="tools",
            declared_interfaces=["fs.read-file"],
            source_language="typescript",
        )
        code = generate_adapter_template(spec, manifest)
        compile(code, "<generated>", "exec")
        assert "async def _handle_fs_read_file(" in code
        # The declared tool name is preserved verbatim on the ToolMetadata.
        assert 'name="fs.read-file"' in code

    def test_template_instantiable_and_conforms(self) -> None:
        """The generated class can be exec'd, instantiated, and conforms."""
        from leapflow.learning.compatibility.adapter_generator import (
            generate_adapter_template,
        )
        from leapflow.plugins.protocol import ToolPlugin

        spec, manifest = _sample_adapter_inputs()
        code = generate_adapter_template(spec, manifest)

        namespace: dict = {}
        exec(compile(code, "<generated>", "exec"), namespace)
        cls = namespace["DshWebSearchBridgePlugin"]
        instance = cls()
        assert instance.plugin_id == "dsh_web_search_bridge"
        assert instance.category == "bridge"
        assert len(instance.tools) == 2
        assert isinstance(instance, ToolPlugin)


class TestAdapterGeneratorLLM:
    """Tests for generate_adapter_with_llm() (optional enhancement)."""

    def test_no_provider_returns_template(self) -> None:
        """When llm_provider is None, output equals the template."""
        from leapflow.learning.compatibility.adapter_generator import (
            generate_adapter_template,
            generate_adapter_with_llm,
        )

        spec, manifest = _sample_adapter_inputs()
        template = generate_adapter_template(spec, manifest)
        result = generate_adapter_with_llm(spec, manifest, None)
        assert result == template

    def test_llm_provider_refines_template(self) -> None:
        """A fake provider returning valid code is used over the template."""
        from leapflow.learning.compatibility.adapter_generator import (
            generate_adapter_with_llm,
        )

        refined = (
            "# refined by llm\n"
            "class RefinedAdapter:\n"
            "    pass\n"
        )

        class FakeProvider:
            def generate(self, prompt: str) -> str:
                return "```python\n" + refined + "```"

        spec, manifest = _sample_adapter_inputs()
        result = generate_adapter_with_llm(spec, manifest, FakeProvider())
        assert "RefinedAdapter" in result
        assert "```" not in result

    def test_llm_invalid_code_falls_back(self) -> None:
        """A provider returning code that does not compile falls back."""
        from leapflow.learning.compatibility.adapter_generator import (
            generate_adapter_template,
            generate_adapter_with_llm,
        )

        class BrokenProvider:
            def generate(self, prompt: str) -> str:
                return "def broken( : this is not python"

        spec, manifest = _sample_adapter_inputs()
        template = generate_adapter_template(spec, manifest)
        result = generate_adapter_with_llm(spec, manifest, BrokenProvider())
        assert result == template

    def test_llm_empty_output_falls_back(self) -> None:
        """A provider returning empty output falls back to template."""
        from leapflow.learning.compatibility.adapter_generator import (
            generate_adapter_template,
            generate_adapter_with_llm,
        )

        class EmptyProvider:
            def generate(self, prompt: str) -> str:
                return "   "

        spec, manifest = _sample_adapter_inputs()
        template = generate_adapter_template(spec, manifest)
        result = generate_adapter_with_llm(spec, manifest, EmptyProvider())
        assert result == template

    def test_llm_provider_raises_falls_back(self) -> None:
        """A provider that raises degrades gracefully to the template."""
        from leapflow.learning.compatibility.adapter_generator import (
            generate_adapter_template,
            generate_adapter_with_llm,
        )

        class RaisingProvider:
            def generate(self, prompt: str) -> str:
                raise RuntimeError("provider down")

        spec, manifest = _sample_adapter_inputs()
        template = generate_adapter_template(spec, manifest)
        result = generate_adapter_with_llm(spec, manifest, RaisingProvider())
        assert result == template

    def test_llm_unsupported_provider_falls_back(self) -> None:
        """A provider with no supported generation method falls back."""
        from leapflow.learning.compatibility.adapter_generator import (
            generate_adapter_template,
            generate_adapter_with_llm,
        )

        class NoMethodProvider:
            pass

        spec, manifest = _sample_adapter_inputs()
        template = generate_adapter_template(spec, manifest)
        result = generate_adapter_with_llm(spec, manifest, NoMethodProvider())
        assert result == template

    def test_llm_async_achat_provider(self) -> None:
        """An async achat-style provider is supported."""
        from leapflow.learning.compatibility.adapter_generator import (
            generate_adapter_with_llm,
        )

        refined = "class AsyncRefined:\n    pass\n"

        class AsyncProvider:
            async def achat(self, messages, stream=False):
                return refined

        spec, manifest = _sample_adapter_inputs()
        result = generate_adapter_with_llm(spec, manifest, AsyncProvider())
        assert "AsyncRefined" in result


class TestAdapterGeneratorEscaping:
    """Special characters in manifest fields must still produce valid Python."""

    def test_adapter_template_handles_special_chars_in_name(self) -> None:
        """Plugin names/interfaces with dots, quotes, slashes produce valid Python."""
        from leapflow.learning.compatibility.adapter_generator import (
            generate_adapter_template,
        )

        spec = AdapterSpec(
            source_interface="io.read",
            target_protocol="ToolPlugin",
            bridge_type="json_rpc_bridge",
            shim_methods=["io.read-file", 'say"hello'],
            estimated_complexity="medium",
        )
        manifest = PluginManifestInput(
            name='@org/dsh-io.tools"v2',
            version="1.0",
            category="tools",
            declared_interfaces=["io.read-file", 'say"hello', "weird\nname"],
            source_language="typescript",
            raw_manifest={"main": 'bridge".js'},
        )
        code = generate_adapter_template(spec, manifest)
        # The internal compile() guard already ran; this asserts it end-to-end.
        compile(code, "<test>", "exec")  # Must not raise
        assert "class Dsh" in code
        assert "plugin_id" in code

    def test_adapter_template_special_chars_instantiable(self) -> None:
        """The escaped adapter can be exec'd and instantiated without error."""
        from leapflow.learning.compatibility.adapter_generator import (
            generate_adapter_template,
        )

        spec = AdapterSpec(
            source_interface="io.read",
            target_protocol="ToolPlugin",
            bridge_type="json_rpc_bridge",
            estimated_complexity="low",
        )
        manifest = PluginManifestInput(
            name='@org/dsh-io.tools"v2',
            version="1.0",
            category="tools",
            declared_interfaces=["io.read-file", 'say"hello'],
            source_language="typescript",
        )
        code = generate_adapter_template(spec, manifest)
        namespace: dict = {}
        exec(compile(code, "<test>", "exec"), namespace)
        cls = namespace["DshIoToolsV2BridgePlugin"]
        instance = cls()
        assert instance.plugin_id == "dsh_io_tools_v2_bridge"
        assert len(instance.tools) == 2
