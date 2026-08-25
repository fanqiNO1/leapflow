"""Shared leapbench helpers: sandbox image presets."""

from enum import Enum
from typing import Any


class LeapImage(str, Enum):
    """Sandbox image presets, selectable by name from CLI or task config."""

    LINUX = "linux"
    # WINDOWS = "windows"
    # MACOS = "macos"


CUA_MCP_PORT = 3000  # Port exposed by the CUA driver for MCP access

# Image spec dicts consumed by cua.Image.from_dict(). Install layers
# (apt_install, env, ...) stay empty until the app UI stack is chosen.
# Host feasibility: LINUX runs under local QEMU+KVM; WINDOWS is untested;
# MACOS requires an Apple Silicon host (Lume).
LINUX_IMAGE_CONFIG: dict[str, Any] = {
    "os_type": "linux",
    "distro": "ubuntu",
    "version": "24.04",
    "kind": "vm",
}

LEAPIMAGE_CONFIGS: dict[LeapImage, dict[str, Any]] = {
    LeapImage.LINUX: LINUX_IMAGE_CONFIG,
}


def get_image_config(image: LeapImage) -> dict[str, Any]:
    """Return a copy of the preset spec for cua.Image.from_dict().

    Shared conventions (e.g. exposing the MCP port) are applied by the caller.
    """
    return LEAPIMAGE_CONFIGS[image].copy()
