"""Stage 1: Manifest Parser.

Parses raw manifest input (dict) into a PluginManifestInput.
Supports two formats:
  - LeapFlow format: dict with keys like name, version, entry_point, checksum_sha256
  - DSH format: dict resembling a package.json with keys like main, keywords, dependencies
"""

from __future__ import annotations

from typing import Any, List

from leapflow.learning.compatibility.protocol import PluginManifestInput, StageResult, Verdict


class ManifestParser:
    """Parse and normalize raw manifest dicts into PluginManifestInput."""

    stage_name: str = "manifest_parser"

    def assess(
        self, manifest: PluginManifestInput, prior_results: List[StageResult]
    ) -> StageResult:
        """Validate the already-parsed manifest for completeness.

        In the pipeline flow, the raw dict is first parsed via parse_raw(),
        then this assess() validates the result.
        """
        # Validate required fields
        if not manifest.name:
            return StageResult(
                stage_name=self.stage_name,
                passed=False,
                verdict=None,
                details="Missing required field: name",
            )
        if not manifest.version:
            return StageResult(
                stage_name=self.stage_name,
                passed=False,
                verdict=None,
                details="Missing required field: version",
            )
        if not manifest.category:
            return StageResult(
                stage_name=self.stage_name,
                passed=False,
                verdict=None,
                details="Missing required field: category",
            )
        return StageResult(
            stage_name=self.stage_name,
            passed=True,
            verdict=None,
            details=f"Manifest parsed successfully (format={manifest.source_format})",
            evidence={"manifest": manifest},
        )

    @staticmethod
    def parse_raw(raw: dict[str, Any]) -> StageResult:
        """Parse a raw dict into a PluginManifestInput.

        Detects format automatically:
          - Presence of 'main' or 'keywords' → DSH format
          - Presence of 'entry_point' or 'checksum_sha256' → LeapFlow format

        Returns StageResult with the parsed manifest in evidence["manifest"],
        or passed=False with error details.
        """
        if not isinstance(raw, dict):
            return StageResult(
                stage_name="manifest_parser",
                passed=False,
                verdict=None,
                details=f"Expected dict, got {type(raw).__name__}",
            )

        # Detect format
        is_dsh = "main" in raw or "keywords" in raw
        is_leapflow = "entry_point" in raw or "checksum_sha256" in raw

        if is_dsh:
            return ManifestParser._parse_dsh(raw)
        elif is_leapflow:
            return ManifestParser._parse_leapflow(raw)
        else:
            # Attempt LeapFlow format as default (requires at least name)
            if "name" in raw:
                return ManifestParser._parse_leapflow(raw)
            return StageResult(
                stage_name="manifest_parser",
                passed=False,
                verdict=None,
                details="Cannot detect manifest format: missing both DSH markers (main/keywords) and LeapFlow markers (entry_point/checksum_sha256)",
            )

    @staticmethod
    def _parse_dsh(raw: dict[str, Any]) -> StageResult:
        """Parse a DSH (package.json-like) manifest."""
        name = raw.get("name", "")
        version = raw.get("version", "")

        if not name:
            return StageResult(
                stage_name="manifest_parser",
                passed=False,
                verdict=None,
                details="DSH manifest missing required field: name",
            )
        if not version:
            return StageResult(
                stage_name="manifest_parser",
                passed=False,
                verdict=None,
                details="DSH manifest missing required field: version",
            )

        # Extract category from keywords, dsh metadata section, or leapflow section
        category = _extract_dsh_category(raw)

        # Extract dependencies
        deps_raw = raw.get("dependencies", {})
        declared_deps = list(deps_raw.keys()) if isinstance(deps_raw, dict) else []

        # Extract interfaces from dsh/leapflow metadata
        metadata = raw.get("dsh", raw.get("leapflow", {}))
        declared_interfaces = metadata.get("interfaces", []) if isinstance(metadata, dict) else []

        # Permissions from metadata
        permissions = metadata.get("permissions", []) if isinstance(metadata, dict) else []

        # Config schema
        config_schema = metadata.get("config", {}) if isinstance(metadata, dict) else {}

        # Execution model
        exec_model = metadata.get("execution_model", "async") if isinstance(metadata, dict) else "async"

        manifest = PluginManifestInput(
            name=name,
            version=version,
            category=category,
            declared_interfaces=declared_interfaces,
            declared_dependencies=declared_deps,
            config_schema=config_schema,
            execution_model=exec_model,
            permissions=permissions,
            source_language="typescript",
            raw_manifest=raw,
            source_format="dsh",
        )

        return StageResult(
            stage_name="manifest_parser",
            passed=True,
            verdict=None,
            details=f"DSH manifest parsed: {name}@{version} (category={category})",
            evidence={"manifest": manifest},
        )

    @staticmethod
    def _parse_leapflow(raw: dict[str, Any]) -> StageResult:
        """Parse a LeapFlow-native manifest."""
        name = raw.get("name", "")
        version = raw.get("version", "")

        if not name:
            return StageResult(
                stage_name="manifest_parser",
                passed=False,
                verdict=None,
                details="LeapFlow manifest missing required field: name",
            )
        if not version:
            return StageResult(
                stage_name="manifest_parser",
                passed=False,
                verdict=None,
                details="LeapFlow manifest missing required field: version",
            )

        # Extract category from metadata or x_leapflow
        metadata = raw.get("metadata", raw.get("x_leapflow", {}))
        category = ""
        if isinstance(metadata, dict):
            category = metadata.get("category", "")
        if not category:
            category = raw.get("category", "tools")

        # Interfaces
        declared_interfaces = raw.get("declared_interfaces", [])

        # Dependencies
        declared_deps = raw.get("dependencies", raw.get("declared_dependencies", []))
        if isinstance(declared_deps, dict):
            declared_deps = list(declared_deps.keys())

        # Permissions
        permissions = raw.get("permissions", [])
        if isinstance(permissions, str):
            permissions = [permissions]

        # Config
        config_schema = raw.get("config_schema", {})

        # Execution model
        exec_model = raw.get("execution_model", "async")

        # Source language
        source_language = raw.get("source_language", raw.get("runtime", "python"))

        manifest = PluginManifestInput(
            name=name,
            version=version,
            category=category,
            declared_interfaces=declared_interfaces,
            declared_dependencies=declared_deps if isinstance(declared_deps, list) else [],
            config_schema=config_schema,
            execution_model=exec_model,
            permissions=permissions,
            source_language=source_language,
            raw_manifest=raw,
            source_format="leapflow",
        )

        return StageResult(
            stage_name="manifest_parser",
            passed=True,
            verdict=None,
            details=f"LeapFlow manifest parsed: {name}@{version} (category={category})",
            evidence={"manifest": manifest},
        )


def _extract_dsh_category(raw: dict[str, Any]) -> str:
    """Extract category from DSH manifest using multiple heuristics.

    Priority:
    1. Explicit category in dsh/leapflow metadata section
    2. First relevant keyword from keywords array
    3. Inferred from package name prefix (dsh-<category>-*)
    4. Fallback to empty string
    """
    # 1. Explicit metadata
    metadata = raw.get("dsh", raw.get("leapflow", {}))
    if isinstance(metadata, dict) and metadata.get("category"):
        return metadata["category"]

    # 2. Keywords
    keywords = raw.get("keywords", [])
    if isinstance(keywords, list) and keywords:
        # Return the first keyword as category hint
        for kw in keywords:
            if isinstance(kw, str) and kw:
                return kw
        return ""

    # 3. Package name heuristic
    name = raw.get("name", "")
    if isinstance(name, str):
        # Strip org prefix like @deepseek-ai/
        if "/" in name:
            name = name.split("/", 1)[1]
        # Strip dsh- prefix and take first segment
        if name.startswith("dsh-"):
            parts = name[4:].split("-", 1)
            if parts:
                return parts[0]

    return ""
