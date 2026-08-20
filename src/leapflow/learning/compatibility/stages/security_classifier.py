"""Stage 6: Security Classifier.

Assesses security risk from declared permissions and recommends
isolation level. Maps permissions to SecurityRisk levels and produces
a recommendation for execution isolation.
"""

from __future__ import annotations

from typing import List

from leapflow.learning.compatibility.protocol import (
    PluginManifestInput,
    SecurityRisk,
    StageResult,
    Verdict,
)

# ═══════════════════════════════════════════════════════════════════════
# Permission → SecurityRisk mapping.
# Uses substring matching for flexibility with varied naming conventions.
# ═══════════════════════════════════════════════════════════════════════

_PERMISSION_RISK_MAP: dict[str, SecurityRisk] = {
    # LOW risk — read-only operations
    "fs.read": SecurityRisk.LOW,
    "filesystem.read": SecurityRisk.LOW,
    "read": SecurityRisk.LOW,
    "config.read": SecurityRisk.LOW,
    "env.read": SecurityRisk.LOW,
    # MEDIUM risk — write operations and outbound network
    "fs.write": SecurityRisk.MEDIUM,
    "filesystem.write": SecurityRisk.MEDIUM,
    "filesystem_write": SecurityRisk.MEDIUM,
    "write": SecurityRisk.MEDIUM,
    "network.outbound": SecurityRisk.MEDIUM,
    "network_outbound": SecurityRisk.MEDIUM,
    "network.connect": SecurityRisk.MEDIUM,
    "http": SecurityRisk.MEDIUM,
    "net": SecurityRisk.MEDIUM,
    # HIGH risk — shell execution and process management
    "shell.execute": SecurityRisk.HIGH,
    "shell_execute": SecurityRisk.HIGH,
    "shell": SecurityRisk.HIGH,
    "process.spawn": SecurityRisk.HIGH,
    "process": SecurityRisk.HIGH,
    "subprocess": SecurityRisk.HIGH,
    "exec": SecurityRisk.HIGH,
    # CRITICAL risk — credential access and system modification
    "credential.access": SecurityRisk.CRITICAL,
    "credential_access": SecurityRisk.CRITICAL,
    "credentials": SecurityRisk.CRITICAL,
    "secrets": SecurityRisk.CRITICAL,
    "system.modify": SecurityRisk.CRITICAL,
    "system_modify": SecurityRisk.CRITICAL,
    "kernel": SecurityRisk.CRITICAL,
    "root": SecurityRisk.CRITICAL,
    "admin": SecurityRisk.CRITICAL,
    "sudo": SecurityRisk.CRITICAL,
}

# Isolation recommendations based on risk level
_ISOLATION_RECOMMENDATION: dict[SecurityRisk, str] = {
    SecurityRisk.LOW: "in_process",
    SecurityRisk.MEDIUM: "in_process",
    SecurityRisk.HIGH: "sandbox",
    SecurityRisk.CRITICAL: "sandbox",
}

# Risk ordering for comparison
_RISK_ORDER: dict[SecurityRisk, int] = {
    SecurityRisk.LOW: 0,
    SecurityRisk.MEDIUM: 1,
    SecurityRisk.HIGH: 2,
    SecurityRisk.CRITICAL: 3,
}


def _classify_permission(permission: str) -> SecurityRisk:
    """Classify a single permission string to a risk level."""
    perm_lower = permission.lower().strip()

    # Exact match
    if perm_lower in _PERMISSION_RISK_MAP:
        return _PERMISSION_RISK_MAP[perm_lower]

    # Substring match
    for known, risk in _PERMISSION_RISK_MAP.items():
        if known in perm_lower or perm_lower in known:
            return risk

    # Default: MEDIUM for unknown permissions (conservative)
    return SecurityRisk.MEDIUM


class SecurityClassifier:
    """Classify security risk of a plugin based on declared permissions."""

    stage_name: str = "security_classifier"

    def assess(
        self, manifest: PluginManifestInput, prior_results: List[StageResult]
    ) -> StageResult:
        """Assess security risk from permissions and recommend isolation.

        - No permissions → LOW risk, in_process
        - CRITICAL permissions from untrusted source → recommend sandbox, possible rejection
        - Aggregate to highest risk level across all permissions
        """
        permissions = manifest.permissions
        if not permissions:
            return StageResult(
                stage_name=self.stage_name,
                passed=True,
                verdict=None,
                details="No permissions declared; low risk",
                evidence={
                    "permissions": [],
                    "risk_level": SecurityRisk.LOW.value,
                    "isolation": "in_process",
                    "classification": {},
                },
            )

        classification: dict[str, str] = {}
        highest_risk = SecurityRisk.LOW

        for perm in permissions:
            risk = _classify_permission(perm)
            classification[perm] = risk.value
            if _RISK_ORDER[risk] > _RISK_ORDER[highest_risk]:
                highest_risk = risk

        isolation = _ISOLATION_RECOMMENDATION[highest_risk]

        # Determine if source is untrusted (DSH format without verification)
        is_untrusted = manifest.source_format == "dsh"

        evidence = {
            "permissions": permissions,
            "risk_level": highest_risk.value,
            "isolation": isolation,
            "classification": classification,
            "is_untrusted_source": is_untrusted,
        }

        # CRITICAL + untrusted → recommend rejection
        if highest_risk == SecurityRisk.CRITICAL and is_untrusted:
            return StageResult(
                stage_name=self.stage_name,
                passed=False,
                verdict=Verdict.INCOMPATIBLE,
                details=(
                    f"CRITICAL permissions ({[p for p in permissions if classification[p] == 'critical']}) "
                    "from untrusted source; recommend rejection"
                ),
                evidence={**evidence, "recommendation": "reject"},
            )

        # HIGH risk → passed but recommend sandbox
        if highest_risk in (SecurityRisk.HIGH, SecurityRisk.CRITICAL):
            return StageResult(
                stage_name=self.stage_name,
                passed=True,
                verdict=Verdict.ADAPTABLE,
                details=(
                    f"Risk level {highest_risk.value}; recommend sandbox isolation. "
                    f"Permissions: {permissions}"
                ),
                evidence={**evidence, "recommendation": "sandbox"},
            )

        # MEDIUM or LOW — pass cleanly
        return StageResult(
            stage_name=self.stage_name,
            passed=True,
            verdict=None,
            details=f"Risk level {highest_risk.value}; standard {isolation} execution acceptable",
            evidence=evidence,
        )
