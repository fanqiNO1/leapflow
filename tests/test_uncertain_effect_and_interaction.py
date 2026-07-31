"""Guards for uncertain-effect reporting and InteractionRequest surfacing.

Two contracts that only hold end-to-end:

- A failed call whose effect may already have landed must say so in its result,
  so the next turn verifies instead of blindly repeating it. An error is not
  proof that nothing happened: an outbound send can time out after delivery.
- A terminal decision carrying an ``InteractionRequest`` must surface it. If the
  engine falls back to ``decision.reason``, the user is told a turn stopped
  without being told what to do about it.
"""

from __future__ import annotations

import pytest

from leapflow.engine.engine import (
    _annotate_uncertain_effect,
    _interaction_metadata,
    _terminal_failure_text,
)
from leapflow.engine.failure_envelope import (
    FailureContext,
    FailureEnvelope,
    FailureSource,
    Recoverability,
    SideEffectState,
)
from leapflow.engine.interaction_request import (
    InteractionRequest,
    InteractionType,
    Severity,
    SuggestedAction,
)
from leapflow.engine.recovery_budget import RecoveryBudget
from leapflow.engine.recovery_coordinator import RecoveryCoordinator
from leapflow.engine.recovery_decision import RecoveryAction, RecoveryDecision
from leapflow.engine.recovery_strategies import default_strategies
from leapflow.engine.tool_execution import effect_is_uncertain_on_failure

# ── Uncertain-effect reporting ───────────────────────────────────────────


@pytest.mark.parametrize("policy", ["external_side_effect", "mutating_once"])
def test_failed_side_effect_is_reported_as_uncertain(policy: str) -> None:
    """The model must be told the effect may have landed despite the error."""
    result = _annotate_uncertain_effect({"ok": False, "error": "timeout"}, policy)

    assert result["side_effect_uncertain"] is True
    assert "retry" in result["retry_guidance"].lower()


@pytest.mark.parametrize("policy", ["read_only", "mutating_idempotent"])
def test_safe_policies_are_not_flagged(policy: str) -> None:
    """Read-only and idempotent failures stay freely retryable.

    Flagging an idempotent mutation would stall a retry that converges anyway.
    """
    result = _annotate_uncertain_effect({"ok": False, "error": "timeout"}, policy)

    assert "side_effect_uncertain" not in result
    assert "retry_guidance" not in result


def test_success_is_never_flagged() -> None:
    """Only failures are ambiguous; a success already reported its outcome."""
    result = _annotate_uncertain_effect({"ok": True}, "external_side_effect")

    assert "side_effect_uncertain" not in result


def test_non_failures_are_not_flagged() -> None:
    """``counts_as_failure=False`` results are not failed attempts."""
    result = _annotate_uncertain_effect(
        {"ok": False, "counts_as_failure": False}, "external_side_effect"
    )

    assert "side_effect_uncertain" not in result


def test_uncertain_policy_set_matches_the_gating_helper() -> None:
    """The helper and the policy set must not drift apart."""
    assert effect_is_uncertain_on_failure("external_side_effect") is True
    assert effect_is_uncertain_on_failure("mutating_once") is True
    assert effect_is_uncertain_on_failure("mutating_idempotent") is False
    assert effect_is_uncertain_on_failure("read_only") is False
    assert effect_is_uncertain_on_failure("") is False


def test_uncertainty_fields_survive_tool_metadata_extraction() -> None:
    """The verdict must reach the transcript, not be filtered out.

    The metadata extractor is an allow-list, so a new field is dropped unless it
    is listed; that would silently undo the annotation.
    """
    from leapflow.engine.engine import AgentEngine

    metadata = AgentEngine._tool_execution_metadata({
        "ok": False,
        "execution_policy": "external_side_effect",
        "side_effect_uncertain": True,
    })

    assert metadata["side_effect_uncertain"] is True


# ── InteractionRequest surfacing ─────────────────────────────────────────


def _envelope() -> FailureEnvelope:
    return FailureEnvelope.create(
        source=FailureSource.TOOL,
        category="tool_timeout",
        failure_class="transient",
        failure_code="timeout",
        message="timed out",
        recoverability=Recoverability.AUTO_RETRY,
        side_effect_state=SideEffectState.UNKNOWN,
        context=FailureContext.from_dict_args(tool_name="gateway_send"),
    )


def _decision_with_interaction() -> RecoveryDecision:
    interaction = InteractionRequest.create(
        interaction_type=InteractionType.RETRY_CHOICE,
        severity=Severity.WARNING,
        title="Delivery may already have happened",
        description="The send timed out after the request was accepted.",
        suggested_actions=(
            SuggestedAction(label="Check the chat, then resend", command="/board", is_default=True),
            SuggestedAction(label="Skip this step"),
        ),
        context={"tool_name": "gateway_send"},
        resumption_key="rk-1",
    )
    return RecoveryDecision.create(
        envelope=_envelope(),
        action=RecoveryAction.ASK_USER,
        reason="internal: blocked replay after uncertain effect",
        strategy_key="test",
        interaction=interaction,
    )


def test_terminal_text_renders_the_interaction_not_the_raw_reason() -> None:
    """The user needs the title and options, not the audit-log sentence."""
    text = _terminal_failure_text(_decision_with_interaction())

    assert "Delivery may already have happened" in text
    assert "The send timed out after the request was accepted." in text
    assert "Check the chat, then resend" in text
    assert "Skip this step" in text
    assert "internal: blocked replay" not in text


def test_terminal_text_falls_back_to_reason_without_an_interaction() -> None:
    """Plain halts keep their existing message."""
    decision = RecoveryDecision.create(
        envelope=_envelope(),
        action=RecoveryAction.HALT_CLEAN,
        reason="Recovery budget exhausted",
        strategy_key="<terminal>",
    )

    assert _terminal_failure_text(decision) == "Recovery budget exhausted"


def test_interaction_metadata_is_structured_for_the_client() -> None:
    """The client must be able to prompt and resume without parsing prose."""
    payload = _interaction_metadata(_decision_with_interaction())["interaction"]

    assert payload["interaction_type"] == "retry_choice"
    assert payload["severity"] == "warning"
    assert payload["resumption_key"] == "rk-1"
    assert payload["context"]["tool_name"] == "gateway_send"
    labels = [action["label"] for action in payload["suggested_actions"]]
    assert "Check the chat, then resend" in labels
    assert payload["suggested_actions"][0]["is_default"] is True


def test_interaction_metadata_is_empty_without_an_interaction() -> None:
    """No interaction means no metadata key to confuse the client."""
    decision = RecoveryDecision.create(
        envelope=_envelope(),
        action=RecoveryAction.HALT_CLEAN,
        reason="halted",
        strategy_key="<terminal>",
    )

    assert _interaction_metadata(decision) == {}


def test_gated_halt_reaches_the_user_with_actionable_text() -> None:
    """The gate's decision must render through the same surfacing path."""
    coord = RecoveryCoordinator(
        strategies=default_strategies(), budget=RecoveryBudget(total_recovery_actions=8),
    )
    coord.budget.start_deadline()

    decision = coord.evaluate(_envelope())
    text = _terminal_failure_text(decision)

    assert decision.action == RecoveryAction.HALT_WITH_CHECKPOINT
    assert "gateway_send" in text
    assert "Verify the target state" in text
    assert _interaction_metadata(decision)["interaction"]["resumption_key"]
