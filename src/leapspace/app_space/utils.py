"""Shared leapspace helpers: image presets, the state-dir convention, and
task action loading."""

import importlib.util
import os
import platform
from enum import Enum
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Awaitable, Callable, Literal

from cua_sandbox import Image

if TYPE_CHECKING:
    from leapspace.app_space.actor import LeapAppActor


def write_atomic(path: Path, text: str) -> None:
    """Write text atomically via a sibling tmp file + os.replace."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def check(name: str, cond: bool, detail: str = "") -> bool:
    """Report one named assertion as a PASS/FAIL line; returns cond.

    Shared convention for task expect() functions: accumulate with
    ``ok &= check(...)`` so every check runs and reports, then exit
    non-zero if any failed.
    """
    print(f"{'PASS' if cond else 'FAIL'} {name}: {detail}")
    return cond


def load_action(
    action_path: Path | str,
) -> tuple[Callable[["LeapAppActor"], Awaitable[None]], Callable[..., int]]:
    """Import a task's action.py once; return its (reference, expect) pair.

    Module-level imports are lint-guaranteed in-sandbox-safe (stdlib /
    PyQt6 / leapspace) — a set the host import satisfies as well — and
    exec_module honors the __main__ guard, so loading never triggers the
    verdict.
    """
    action_path = Path(action_path)
    spec = importlib.util.spec_from_file_location(
        f"leapspace_task_{action_path.stem}", str(action_path)
    )
    if spec is None or spec.loader is None:
        raise ValueError(f"{action_path}: cannot be imported as a Python module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.reference, module.expect


class LeapAppImage(str, Enum):
    """Sandbox image presets, selectable by name from CLI or task config.

    Values are the sandbox OS names, which get_sandbox_state_dir keys on.
    """

    LINUX = "linux"
    # WINDOWS = "windows"
    # MACOS = "macos"


# Fixed per-OS roots, never resolved and never env-overridden: the harness
# and the in-sandbox apps compute this independently and must land on the
# same literal path.
_STATE_DIR_BY_SYSTEM: dict[str, PurePath] = {
    "linux": PurePosixPath("/tmp/leapspace"),
    "macos": PurePosixPath("/tmp/leapspace"),
    "windows": PureWindowsPath("C:/ProgramData/leapspace"),
}


def get_sandbox_state_dir(
    in_sandbox: bool,
    system: Literal["linux", "macos", "windows"] | None = None,
) -> PurePath:
    """The apps' state root — the one path harness and apps must agree on.

    Two consumers, two ways to know the sandbox OS: apps run inside and
    detect it with platform.system(); the harness runs on the host, where
    detection would answer the host's OS, so it names the sandbox's OS
    (an image preset's value) instead. Callers append the app_id. No env
    override — hermetic tests monkeypatch this function.
    """
    if in_sandbox:
        system = platform.system().lower()
        system = "macos" if system == "darwin" else system
    else:
        if system is None:
            raise ValueError("system is required when resolving from the host")
    return _STATE_DIR_BY_SYSTEM[system]


CUA_MCP_PORT = 3000  # Port exposed by the CUA driver for MCP access

# System X/GL libraries the pip PyQt6 wheel needs (it bundles Qt itself).
PYQT_SYSTEM_LIBS = [
    "libegl1",
    "libgl1",
    "libxkbcommon0",
    "libxkbcommon-x11-0",
    "libfontconfig1",
    "libglib2.0-0t64",
    "libdbus-1-3",
    "libxcb-cursor0",
    "libxcb-icccm4",
    "libxcb-image0",
    "libxcb-keysyms1",
    "libxcb-randr0",
    "libxcb-render-util0",
    "libxcb-shape0",
    "libxcb-xinerama0",
    "libxcb-xkb1",
    "libx11-xcb1",
]

# Repo checkout path inside the sandbox image.
LINUX_LEAPFLOW_PATH = "/opt/leapflow"

# Image spec dicts consumed by cua.Image.from_dict(). Install layers
# (apt_install, env, ...) stay empty until the app UI stack is chosen.
# Host feasibility: LINUX runs under local QEMU+KVM; WINDOWS is untested;
# MACOS requires an Apple Silicon host (Lume).
LINUX_IMAGE_CONFIG: Image = (Image.linux(distro="ubuntu", version="24.04", kind="vm")
                             .expose(CUA_MCP_PORT)
                             .apt_install("python3-pyatspi", *PYQT_SYSTEM_LIBS, "git", "make")
                             .pip_install("PyQt6", "uv")
                             .run(f"git clone https://github.com/modelscope/leapflow.git {LINUX_LEAPFLOW_PATH}")
                             .run(f"cd {LINUX_LEAPFLOW_PATH} && make space-sync"))

IMAGE_CONFIGS: dict[LeapAppImage, Image] = {
    LeapAppImage.LINUX: LINUX_IMAGE_CONFIG,
}


def get_image(image: LeapAppImage) -> Image:
    """Return the preset image spec.

    Image is frozen and chainable — every mutation returns a new instance —
    so the shared preset can be handed out directly.
    """
    return IMAGE_CONFIGS[image]


def get_image_python(system: Literal["linux", "macos", "windows"]) -> str:
    """Interpreter inside the image's repo venv, keyed by sandbox OS.

    The venv is built by the image's `make space-sync` step (uv sync,
    hence the .venv name). The path must be spelled out: the shell cwd
    is not the repo checkout, so interpreter discovery (uv run, bare
    python) would resolve elsewhere. The harness launches apps and
    runs the verdict program through this interpreter.
    """
    if system == "linux":
        return f"{LINUX_LEAPFLOW_PATH}/.venv/bin/python"
    raise NotImplementedError(f"image python path not defined for {system}")
