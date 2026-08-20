"""Plugin Compatibility Assessment Engine.

Evaluates foreign plugins (primarily from deepseek-harness ecosystem)
for LeapFlow compatibility before installation is attempted.
"""

from leapflow.learning.compatibility.pipeline import assess_plugin
from leapflow.learning.compatibility.protocol import (
    CompatibilityReport,
    PluginManifestInput,
    Verdict,
)

__all__ = ["assess_plugin", "CompatibilityReport", "Verdict", "PluginManifestInput"]
