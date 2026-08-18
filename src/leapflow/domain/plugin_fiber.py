"""Plugin lifecycle state machine.

A PluginFiber tracks the lifecycle of a single plugin instance through
its states: PENDING → ACTIVE → UNLOADING → DISPOSED.

Each fiber owns an EffectScope. When the fiber is disposed, its scope
is automatically disposed, triggering all registered cleanup effects.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from leapflow.domain.effect_scope import EffectScope


# Module-level monotonic counter for PluginFiber generation IDs.
# Safe under LeapFlow's single-threaded asyncio model; if multi-threaded
# plugin lifecycle management is added later, this counter must be guarded
# by a threading.Lock or moved to a thread-safe primitive.
_generation_counter: int = 0


def _next_generation() -> int:
    """Return the next monotonic generation number."""
    global _generation_counter
    _generation_counter += 1
    return _generation_counter


class FiberState(enum.Enum):
    """Plugin fiber lifecycle states."""
    PENDING = "pending"
    ACTIVE = "active"
    UNLOADING = "unloading"
    DISPOSED = "disposed"


class IllegalStateTransition(RuntimeError):
    """Raised when a fiber state transition is invalid."""


_VALID_TRANSITIONS: dict[FiberState, set[FiberState]] = {
    FiberState.PENDING: {FiberState.ACTIVE},
    FiberState.ACTIVE: {FiberState.UNLOADING},
    FiberState.UNLOADING: {FiberState.DISPOSED},
    FiberState.DISPOSED: set(),
}


@dataclass
class PluginFiber:
    """Lifecycle state machine for a plugin instance.

    Usage:
        scope = EffectScope("my-plugin")
        fiber = PluginFiber(plugin_id="my-plugin", scope=scope)
        fiber.activate()   # PENDING → ACTIVE
        # ... plugin operates ...
        fiber.begin_unload()  # ACTIVE → UNLOADING
        fiber.dispose()    # UNLOADING → DISPOSED + scope.dispose()
    """

    plugin_id: str
    scope: EffectScope = field(repr=False)
    state: FiberState = FiberState.PENDING
    generation: int = field(default_factory=_next_generation)

    @property
    def is_active(self) -> bool:
        """Whether the fiber is in ACTIVE state."""
        return self.state == FiberState.ACTIVE

    @property
    def is_disposed(self) -> bool:
        """Whether the fiber has been fully disposed."""
        return self.state == FiberState.DISPOSED

    def activate(self) -> None:
        """Transition from PENDING to ACTIVE."""
        self._transition(FiberState.ACTIVE)

    def begin_unload(self) -> None:
        """Transition from ACTIVE to UNLOADING."""
        self._transition(FiberState.UNLOADING)

    def dispose(self) -> None:
        """Transition to DISPOSED and dispose the owned scope."""
        self._transition(FiberState.DISPOSED)
        self.scope.dispose()

    def _transition(self, target: FiberState) -> None:
        if target not in _VALID_TRANSITIONS[self.state]:
            raise IllegalStateTransition(
                f"Cannot transition fiber '{self.plugin_id}' "
                f"from {self.state.value} to {target.value}"
            )
        self.state = target
