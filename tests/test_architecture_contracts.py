"""Executable guards for the architecture contracts in AGENTS.md.

These contracts are the ones a code review is worst at catching, because a
violation looks locally reasonable: one vendor import in a core module, one
mutable domain type, one event-subscription method on a one-shot backend. Each
test below therefore asserts the *boundary* rather than any single call site,
so the guard keeps holding as the implementation moves.

Covered contracts:
- Platform-Neutral Gateway Core (core must not import platform packages)
- Platform vs App Business Boundary (no vendor endpoints/error shapes in core)
- Transport-Lifecycle Separation (one-shot actions vs long-lived observations)
- Immutable Domain Types (frozen dataclasses for domain objects)
- Protocol over ABC (extension points are runtime_checkable Protocols)
- Standalone importability (no import-time side effects)
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import pathlib
import re
import typing

import pytest

GATEWAY_DIR = pathlib.Path(__file__).resolve().parents[1] / "src" / "leapflow" / "gateway"

# Sub-packages that own platform/vendor specifics. Gateway core may define the
# contracts these implement, but must never depend on them.
_PLATFORM_PACKAGES = ("adapters", "normalizers", "action_packs", "backends", "manifests")


def _core_modules() -> list[pathlib.Path]:
    """Return gateway core modules (top-level files, excluding sub-packages)."""
    return sorted(p for p in GATEWAY_DIR.glob("*.py") if p.name != "__init__.py")


def _imported_modules(path: pathlib.Path) -> list[tuple[str, int]]:
    """Return (module, lineno) for every import in a source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found.append((node.module or "", node.lineno))
        elif isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
    return found


# ── Platform-Neutral Gateway Core ────────────────────────────────────────


def test_gateway_core_does_not_import_platform_packages() -> None:
    """Core owns protocols, lifecycle, routing, approval, audit — not vendors.

    A core module importing an adapter/normalizer/action pack inverts the
    dependency and makes every new platform a core change.
    """
    violations: list[str] = []
    for path in _core_modules():
        for module, lineno in _imported_modules(path):
            for package in _PLATFORM_PACKAGES:
                if f"gateway.{package}" in module:
                    violations.append(f"{path.name}:{lineno} imports {module}")

    assert violations == [], (
        "gateway core must not depend on platform packages; move the "
        "platform-specific part behind a protocol or into the adapter:\n  "
        + "\n  ".join(violations)
    )


def test_gateway_core_does_not_import_vendor_sdks() -> None:
    """Vendor SDKs belong to adapters/backends, never to core modules."""
    vendor_sdk_roots = ("lark_oapi", "telebot", "telegram", "slack_sdk", "dingtalk")
    violations: list[str] = []
    for path in _core_modules():
        for module, lineno in _imported_modules(path):
            root = module.split(".")[0]
            if root in vendor_sdk_roots:
                violations.append(f"{path.name}:{lineno} imports {module}")

    assert violations == [], "vendor SDK imported by gateway core:\n  " + "\n  ".join(violations)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known violation: gateway/validators.py keeps the platform-neutral "
        "validator registry together with three vendor implementations that "
        "hardcode Feishu/DingTalk/Telegram endpoints and their distinct error "
        "JSON shapes. Moving them is deliberately deferred: validate_credentials() "
        "returns (True, '') when no validator is registered, so relocating "
        "registration without care would silently skip credential validation. "
        "Tracked here so the debt stays visible instead of allow-listed."
    ),
)
def test_gateway_core_has_no_vendor_endpoints_or_error_shapes() -> None:
    """Vendor wire formats must live in the app's pack/adapter, not in core."""
    vendor_endpoint = re.compile(
        r"https?://[^\s\"']*(feishu|larksuite|dingtalk|telegram|slack)", re.IGNORECASE
    )
    violations: list[str] = []
    for path in _core_modules():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if vendor_endpoint.search(line):
                violations.append(f"{path.name}:{lineno}")

    assert violations == [], "vendor endpoint hardcoded in gateway core: " + ", ".join(violations)


# ── Transport-Lifecycle Separation ───────────────────────────────────────


def test_one_shot_action_backend_exposes_no_event_subscription() -> None:
    """``ExecutionBackend`` runs bounded actions; it must not stream events.

    Merging the two lets a streaming subscriber, webhook, or polling loop be
    implemented inside one-shot action execution, which is the exact coupling
    the contract forbids.
    """
    from leapflow.gateway.connectors.protocol import ExecutionBackend

    members = {name for name in dir(ExecutionBackend) if not name.startswith("_")}
    assert "execute" in members, "ExecutionBackend must run actions"
    assert members.isdisjoint({"events", "start", "stop"}), (
        "ExecutionBackend must not own long-lived observation methods; those "
        f"belong to BackendEventSource. Found: {sorted(members)}"
    )


def test_long_lived_event_source_exposes_no_action_execution() -> None:
    """``BackendEventSource`` observes; it must not execute actions."""
    from leapflow.gateway.connectors.protocol import BackendEventSource

    members = {name for name in dir(BackendEventSource) if not name.startswith("_")}
    assert {"events", "start", "stop"} <= members, "event source must own its lifecycle"
    assert members.isdisjoint({"execute", "preview"}), (
        f"BackendEventSource must not execute actions. Found: {sorted(members)}"
    )


# ── Immutable Domain Types ───────────────────────────────────────────────


_DOMAIN_TYPES = [
    ("leapflow.gateway.protocol", "InboundMessage"),
    ("leapflow.gateway.protocol", "OutboundContent"),
    ("leapflow.gateway.protocol", "SendTarget"),
    ("leapflow.gateway.protocol", "SendResult"),
    ("leapflow.gateway.protocol", "MessageSource"),
    ("leapflow.gateway.connectors.protocol", "ActionSpec"),
    ("leapflow.gateway.connectors.protocol", "ActionResult"),
    ("leapflow.gateway.connectors.protocol", "ActionFailure"),
    ("leapflow.engine.failure_envelope", "FailureEnvelope"),
    ("leapflow.engine.failure_envelope", "FailureContext"),
    ("leapflow.engine.failure_envelope", "RecoveryHint"),
    ("leapflow.engine.recovery_decision", "RecoveryDecision"),
    ("leapflow.engine.recovery_decision", "BackoffConfig"),
    ("leapflow.engine.recovery_decision", "RetrySemantics"),
    ("leapflow.monitor.types", "Finding"),
    ("leapflow.monitor.types", "WatchSpec"),
]


@pytest.mark.parametrize(("module_name", "type_name"), _DOMAIN_TYPES)
def test_domain_types_are_frozen(module_name: str, type_name: str) -> None:
    """Domain objects crossing module boundaries must be immutable.

    These types are passed between engine, gateway, and storage; a mutable one
    lets a downstream consumer edit shared state instead of deriving a new value.
    """
    cls = getattr(importlib.import_module(module_name), type_name)

    assert dataclasses.is_dataclass(cls), f"{type_name} must be a dataclass"
    assert cls.__dataclass_params__.frozen, (
        f"{type_name} is a shared domain type and must be frozen=True"
    )


def test_frozen_domain_type_rejects_mutation_at_runtime() -> None:
    """The frozen flag must actually block writes (not just be declared)."""
    from leapflow.engine.failure_envelope import FailureEnvelope, FailureSource, Recoverability

    envelope = FailureEnvelope.create(
        source=FailureSource.TOOL,
        category="tool_timeout",
        failure_class="transient",
        failure_code="timeout",
        message="timed out",
        recoverability=Recoverability.AUTO_RETRY,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        envelope.message = "rewritten"  # type: ignore[misc]


# ── Protocol over ABC ────────────────────────────────────────────────────


_EXTENSION_POINTS = [
    ("leapflow.gateway.connectors.protocol", "ExecutionBackend"),
    ("leapflow.gateway.connectors.protocol", "BackendEventSource"),
    ("leapflow.engine.recovery_coordinator", "RecoveryStrategy"),
    ("leapflow.monitor.types", "MonitorProducer"),
    ("leapflow.dashboard.service", "DashboardDataProvider"),
]


@pytest.mark.parametrize(("module_name", "type_name"), _EXTENSION_POINTS)
def test_extension_points_are_runtime_checkable_protocols(
    module_name: str, type_name: str,
) -> None:
    """Extension points must be Protocols so implementations stay decoupled.

    ``runtime_checkable`` is part of the contract: registration and test code
    verify conformance with ``isinstance`` rather than by subclassing. Missing
    names fail rather than skip — a renamed extension point must be noticed.
    """
    module = importlib.import_module(module_name)
    cls = getattr(module, type_name, None)

    assert cls is not None, f"{module_name} must export the {type_name} extension point"
    assert issubclass(cls, typing.Protocol), f"{type_name} must be a typing.Protocol"  # type: ignore[arg-type]
    assert getattr(cls, "_is_runtime_protocol", False), (
        f"{type_name} must be decorated with @runtime_checkable"
    )


# ── Standalone importability ─────────────────────────────────────────────


_STANDALONE_MODULES = [
    "leapflow.logging_setup",
    "leapflow.layout",
    "leapflow.config_service",
    "leapflow.gateway.trigger_policy",
    "leapflow.gateway.session_router",
    "leapflow.gateway.validators",
    "leapflow.engine.recovery_coordinator",
    "leapflow.engine.recovery_strategies",
    "leapflow.engine.failure_envelope",
    "leapflow.monitor.types",
    "leapflow.monitor.session_producer",
    "leapflow.dashboard.service",
    "leapflow.daemon.session_registry",
    "leapflow.daemon.notifications",
]


@pytest.mark.parametrize("module_name", _STANDALONE_MODULES)
def test_module_imports_standalone(module_name: str) -> None:
    """Every module must import without side effects or optional deps.

    Guards the graceful-degradation contract at import level: a module that
    needs aiohttp/duckdb/an LLM at import time breaks unrelated entry points.
    """
    assert importlib.import_module(module_name) is not None
