"""Plane-aware reference resolution for session focus.

This resolver does not choose tools and does not route intents. It only resolves
an already-written deictic reference ("the above paper", "the model I just set")
against structured session focus state. That keeps it within the context assembly
layer instead of reintroducing natural-language tool fitting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from leapflow.engine.context_focus import ContextPlane, ReferenceResolution, SessionFocusState

_DEICTIC_RE = re.compile(r"(above|previous|earlier|that|this|it|刚才|上面|上述|前面|这个|那个|该)", re.IGNORECASE)
_TASK_DOCUMENT_RE = re.compile(r"(paper|论文|文章|文档|document|file)", re.IGNORECASE)
_CONTROL_MODEL_RE = re.compile(r"(llm|model|模型|默认模型|设置|配置|config)", re.IGNORECASE)


@dataclass(frozen=True)
class ReferenceResolver:
    """Resolve deictic references using the structured focus state."""

    def resolve(self, user_text: str, state: SessionFocusState) -> ReferenceResolution:
        text = str(user_text or "")
        if not text.strip():
            return ReferenceResolution.unresolved("empty user text")

        has_deictic = bool(_DEICTIC_RE.search(text))
        wants_document = bool(_TASK_DOCUMENT_RE.search(text))
        wants_control_model = bool(_CONTROL_MODEL_RE.search(text)) and (
            "llm" in text.lower()
            or "model" in text.lower()
            or "模型" in text
            or "默认" in text
            or "设置" in text
            or "配置" in text
        )

        if has_deictic and wants_control_model and not wants_document:
            event = state.latest_control_event(key="llm.model") or state.latest_control_event()
            if event is None:
                return ReferenceResolution.unresolved("no matching control-plane event", target_kind="model")
            return ReferenceResolution(
                target_kind="model",
                target_id=f"control:{event.key}:{event.turn_id}",
                target_name=event.value or event.key,
                plane=ContextPlane.CONTROL_PLANE,
                confidence=0.92,
                reason="deictic model/config reference resolved to latest control-plane event",
            )

        if has_deictic and wants_document:
            candidates = state.task_focus_candidates(kind="paper")
            if not candidates:
                return ReferenceResolution.unresolved("no task document focus available", target_kind="paper")
            if len(candidates) > 1 and _ambiguous(candidates):
                return ReferenceResolution.ambiguous("multiple recent task documents are similarly salient", target_kind="paper")
            focus = candidates[0]
            return ReferenceResolution(
                target_kind=focus.kind,
                target_id=focus.entity_id,
                target_name=focus.canonical_name,
                plane=focus.plane,
                confidence=0.9,
                reason="deictic document reference resolved to active task focus",
            )

        focus = state.active_focus
        if has_deictic and focus is not None:
            return ReferenceResolution(
                target_kind=focus.kind,
                target_id=focus.entity_id,
                target_name=focus.canonical_name,
                plane=focus.plane,
                confidence=0.72,
                reason="generic deictic reference resolved to active task focus",
            )

        return ReferenceResolution.unresolved("no deictic reference requiring focus resolution")


def _ambiguous(candidates) -> bool:
    if len(candidates) < 2:
        return False
    first, second = candidates[0], candidates[1]
    return first.last_task_turn == second.last_task_turn and abs(first.salience - second.salience) < 0.05


__all__ = ["ReferenceResolver"]
