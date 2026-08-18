"""Shared domain model — zero-dependency data types used across all layers."""

from leapflow.domain.effect_scope import EffectScope, ScopeState
from leapflow.domain.event_types import (
    CLIEventType,
    ImplicitFeedbackType,
    LearningEventType,
    NormalizedEventType,
    UIActionSubType,
    UNDO_SHORTCUTS,
)
from leapflow.domain.events import SystemEvent, UIElement, UISnapshot
from leapflow.domain.platform import (
    Capability,
    DEFAULT_DARWIN_CAPABILITIES,
    PlatformID,
    PlatformManifest,
    capability_from_str,
)
from leapflow.domain.plugin_fiber import FiberState, IllegalStateTransition, PluginFiber
from leapflow.domain.skill_types import DistillationCandidate, SkillMetadata, SkillParameter
from leapflow.domain.trajectory import (
    ActionType,
    Episode,
    NoiseSignal,
    RawAction,
    RecordingState,
    SemanticAction,
    SnapshotLevel,
    StateSnapshot,
    Trajectory,
    TrajectoryStep,
    action_type_from_event,
)

__all__ = [
    "ActionType",
    "CLIEventType",
    "EffectScope",
    "FiberState",
    "ImplicitFeedbackType",
    "LearningEventType",
    "NormalizedEventType",
    "UIActionSubType",
    "UNDO_SHORTCUTS",
    "Capability",
    "DEFAULT_DARWIN_CAPABILITIES",
    "DistillationCandidate",
    "Episode",
    "IllegalStateTransition",
    "NoiseSignal",
    "PlatformID",
    "PlatformManifest",
    "PluginFiber",
    "RawAction",
    "RecordingState",
    "ScopeState",
    "SemanticAction",
    "SkillMetadata",
    "SkillParameter",
    "SnapshotLevel",
    "StateSnapshot",
    "SystemEvent",
    "Trajectory",
    "TrajectoryStep",
    "UIElement",
    "UISnapshot",
    "action_type_from_event",
    "capability_from_str",
]
