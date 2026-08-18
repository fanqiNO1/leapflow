"""Plugin manifest format for marketplace distribution."""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import List
import hashlib
import json


@dataclass(frozen=True)
class PluginManifest:
    """Metadata describing a distributable plugin.

    A manifest is the contract between a plugin author and the marketplace.
    It carries enough information to discover, verify, and install a plugin.
    """
    name: str                       # unique plugin identifier
    version: str                    # semver
    author: str
    description: str
    entry_point: str                # module path within the package, e.g. "my_plugin"
    plugin_type: str = "tool"       # "tool" | "active_signal_source" | "gateway" | "llm"
    source_url: str = ""            # where to download the code (file:// or https://)
    checksum_sha256: str = ""       # integrity verification
    requires_sandbox: bool = True   # untrusted by default
    dependencies: List[str] = field(default_factory=list)  # other plugin names
    min_leapflow_version: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, data: str) -> "PluginManifest":
        raw = json.loads(data)
        # tolerate extra keys
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def verify_checksum(self, content: bytes) -> bool:
        """Verify content matches the declared checksum."""
        if not self.checksum_sha256:
            return False
        actual = hashlib.sha256(content).hexdigest()
        return actual == self.checksum_sha256

    @staticmethod
    def compute_checksum(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()
