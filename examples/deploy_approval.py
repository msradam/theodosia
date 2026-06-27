"""Deployment-approval FSM: typed inputs and an escalation gate.

Shape:

    open_change(change) --> review --> approve(reason) --> deploy(reason) --> verify
                                  \\-> reject(reason)              \\-> rollback
                                  \\-> abort                 \\-> abort

Features it exercises:

* A nested Pydantic input. ``open_change`` takes ``change: ChangeRequest``
  (a ``BaseModel`` with ``service``, ``risk: Literal["low", "high"]`` and
  ``summary``). Theodosia surfaces ``model_json_schema()`` under
  ``next_action_schemas`` so the caller sees the nested object shape, and
  coerces the dict the client sends into the model before dispatch.
* A ``validation_failed`` path on malformed typed input. A dict that does
  not validate as ``ChangeRequest`` (e.g. ``risk="critical"``) is refused at
  the wire boundary with per-field Pydantic errors, not an action crash.
* An escalation gate. ``deploy`` is guarded by an input validator that
  refuses unless a prior ``approve`` step ran and a non-empty ``reason`` is
  supplied. The refusal is a structured ``validation_failed`` carrying
  ``valid_next_actions`` + ``next_action_schemas`` so the agent recovers.

Eight actions, three terminal stages (``verified``, ``rejected``,
``rolled_back`` / ``aborted``).

Serve over stdio:

    theodosia serve deploy_approval:build --app-dir examples
"""

from __future__ import annotations

import os
from typing import Any, Literal

import pydantic
from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition

from theodosia import ValidationFailed, tracker

_TRACKER_PROJECT = os.environ.get("THEODOSIA_PROJECT", "deploy-dogfood")


class ChangeRequest(pydantic.BaseModel):
    """A change to be deployed. The nested input to ``open_change``."""

    service: str = pydantic.Field(description="Service to deploy, e.g. 'payments'.")
    risk: Literal["low", "high"] = pydantic.Field(description="Risk tier of the change.")
    summary: str = pydantic.Field(min_length=1, description="One-line change summary.")


@action(reads=[], writes=["stage", "service", "risk", "summary", "approved"])
def open_change(state: State, change: ChangeRequest) -> State:
    """Open a deployment change request.

    Args:
        change: A ``ChangeRequest`` object: ``{"service": str,
            "risk": "low" | "high", "summary": str}``. A dict that does not
            validate against that schema is refused as ``validation_failed``.
    """
    return state.update(
        stage="opened",
        service=change.service,
        risk=change.risk,
        summary=change.summary,
        approved=False,
    )


@action(reads=["service", "risk"], writes=["stage", "review_notes"])
def review(state: State) -> State:
    """Review the open change. Branches to approve / reject / abort."""
    risk = state["risk"]
    return state.update(
        stage="reviewed",
        review_notes=f"{state['service']} reviewed; risk={risk}",
    )


@action(reads=["stage"], writes=["stage", "approved", "approve_reason"])
def approve(state: State, reason: str) -> State:
    """Approve the reviewed change.

    Args:
        reason: Why the change is approved. Recorded for the deploy gate.
    """
    return state.update(stage="approved", approved=True, approve_reason=reason)


@action(reads=["stage"], writes=["stage", "reject_reason"])
def reject(state: State, reason: str) -> State:
    """Reject the reviewed change. Terminal.

    Args:
        reason: Why the change is rejected.
    """
    return state.update(stage="rejected", reject_reason=reason)


@action(reads=["stage", "approved"], writes=["stage", "deploy_reason"])
def deploy(state: State, reason: str = "") -> State:
    """Deploy the approved change. Escalation-gated.

    Refused unless a prior ``approve`` step ran (``approved`` is set) and a
    non-empty ``reason`` is supplied. The gate is an input validator, so the
    refusal is a structured ``validation_failed`` the agent can recover from.

    Args:
        reason: Non-empty deployment justification. Required by the gate.
    """
    return state.update(stage="deployed", deploy_reason=reason)


@action(reads=["stage", "service"], writes=["stage", "verify_result"])
def verify(state: State) -> State:
    """Verify the deployment succeeded. Terminal."""
    return state.update(stage="verified", verify_result=f"{state['service']} healthy")


@action(reads=["stage"], writes=["stage"])
def rollback(state: State) -> State:
    """Roll the deployment back. Terminal; reachable after deploy."""
    return state.update(stage="rolled_back")


@action(reads=["stage"], writes=["stage"])
def abort(state: State) -> State:
    """Abort the change. Terminal; reachable pre-deploy."""
    return state.update(stage="aborted")


def _deploy_gate(state: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any] | None:
    """Escalation gate for ``deploy``: refuse unprivileged or unjustified calls."""
    if not state.get("approved"):
        raise ValidationFailed(
            "deploy refused: no prior approval on record",
            details={"requirement": "an approve step must run before deploy"},
        )
    reason = str(inputs.get("reason") or "").strip()
    if not reason:
        raise ValidationFailed(
            "deploy refused: a non-empty 'reason' is required to deploy",
            details={"param": "reason", "requirement": "non-empty deployment justification"},
        )
    return None


deploy._theodosia_validator = _deploy_gate  # type: ignore[attr-defined]


def build() -> ApplicationBuilder:
    """Return the unbuilt deploy-approval builder (the builder seam).

    Returns an ``ApplicationBuilder`` rather than a built ``Application`` so
    Theodosia stamps a per-session ``app_id`` and binds the tracker dir to
    the session before building. Do not set your own ``app_id`` here.
    """
    opened = Condition.expr("stage == 'opened'")
    reviewed = Condition.expr("stage == 'reviewed'")
    approved = Condition.expr("stage == 'approved'")
    deployed = Condition.expr("stage == 'deployed'")
    return (
        ApplicationBuilder()
        .with_actions(
            open_change=open_change,
            review=review,
            approve=approve,
            reject=reject,
            deploy=deploy,
            verify=verify,
            rollback=rollback,
            abort=abort,
        )
        .with_transitions(
            ("open_change", "review", opened),
            ("review", "approve", reviewed),
            ("review", "reject", reviewed),
            ("review", "abort", reviewed),
            ("approve", "deploy", approved),
            ("approve", "abort", approved),
            ("deploy", "verify", deployed),
            ("deploy", "rollback", deployed),
        )
        .with_tracker(tracker(project=_TRACKER_PROJECT))
        .with_state(stage="new")
        .with_entrypoint("open_change")
    )
