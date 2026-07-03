"""Airline rebooking: a real complex-agent surface, gated two ways.

The safe-vs-sensitive tool split and the way the sensitive (mutating) tools are
gated are taken directly from LangGraph's own customer-support tutorial:

    https://github.com/langchain-ai/langgraph/blob/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials/customer-support/customer-support.ipynb

That tutorial classifies tools as `safe_tools` vs `sensitive_tools` and compiles
the graph with ``interrupt_before=["sensitive_tools"]``: before a mutating tool
runs, the graph pauses for human approval. `search_flights` is safe;
`update_ticket_to_new_flight` and `cancel_ticket` are sensitive. We keep the real
tool signatures and a tiny in-memory DB so both renderings run key-free.

The one addition to the real signature is a keyword-only ``confirmed`` flag on
the mutation, so the demo can tell an approved change from an unconfirmed one.

Burr FSM <-> LangGraph: search <-> search_flights (safe); rebook <->
update_ticket_to_new_flight (sensitive); the `confirm` step is the server-side
analog of the tutorial's `interrupt_before=["sensitive_tools"]` pause. `find` is
added to look up the ticket first.
"""

from __future__ import annotations

from typing import Any

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition

from theodosia import ValidationFailed, tracker

TICKETS: dict[str, dict[str, Any]] = {
    "TKT-42": {"passenger": "A. Rahman", "flight_id": 100, "route": "JFK->SFO"},
}
FLIGHTS: dict[int, dict[str, Any]] = {
    100: {"route": "JFK->SFO", "depart": "2026-08-01T08:00", "seats": 0},  # current, full
    205: {"route": "JFK->SFO", "depart": "2026-08-02T09:00", "seats": 4},
    206: {"route": "JFK->SFO", "depart": "2026-08-02T18:00", "seats": 2},
}

# Every mutation the capability actually performed (money/booking side effects).
COMMITS: list[dict[str, Any]] = []


def search_flights(route: str, date: str) -> list[dict[str, Any]]:
    """SAFE tool: read-only flight search."""
    return [
        {"flight_id": fid, **f}
        for fid, f in FLIGHTS.items()
        if f["route"] == route and f["depart"].startswith(date) and f["seats"] > 0
    ]


def update_ticket_to_new_flight(
    ticket_no: str, new_flight_id: int, *, confirmed: bool = False
) -> dict[str, Any]:
    """SENSITIVE tool: mutate the booking. Does not enforce approval itself."""
    TICKETS[ticket_no]["flight_id"] = new_flight_id
    rec = {
        "op": "update_ticket_to_new_flight",
        "ticket_no": ticket_no,
        "new_flight_id": new_flight_id,
        "confirmed": confirmed,
    }
    COMMITS.append(rec)
    return {"ok": True, **rec}


def confirmed_commit(commit: dict[str, Any]) -> bool:
    return bool(commit.get("confirmed"))


# ── the Theodosia FSM: find -> search -> confirm -> rebook ─────────────
@action(reads=[], writes=["stage", "ticket_no", "route", "current_flight"])
def find(state: State, booking_ref: str) -> State:
    t = TICKETS[booking_ref]
    return state.update(
        stage="found", ticket_no=booking_ref, route=t["route"], current_flight=t["flight_id"]
    )


@action(reads=["route"], writes=["stage", "options"])
def search(state: State, date: str) -> State:
    options = [f["flight_id"] for f in search_flights(state["route"], date)]
    return state.update(stage="searched", options=options)


@action(reads=["stage"], writes=["stage", "confirmed"])
def confirm(state: State, acknowledge: str) -> State:
    """The server-side analog of LangGraph's interrupt: an explicit confirm step."""
    return state.update(stage="confirmed", confirmed=True)


@action(reads=["ticket_no", "options", "confirmed"], writes=["stage", "result"])
def rebook(state: State, new_flight_id: int) -> State:
    """SENSITIVE: reachable only after confirm, and only for a searched flight."""
    result = update_ticket_to_new_flight(state["ticket_no"], new_flight_id, confirmed=True)
    return state.update(stage="rebooked", result=result)


def _find_gate(state: dict, inputs: dict) -> dict | None:
    if inputs.get("booking_ref") not in TICKETS:
        raise ValidationFailed(
            f"unknown booking ref {inputs.get('booking_ref')!r}", details={"param": "booking_ref"}
        )
    return None


def _confirm_gate(state: dict, inputs: dict) -> dict | None:
    if not str(inputs.get("acknowledge") or "").strip():
        raise ValidationFailed(
            "a confirmation acknowledgement is required", details={"param": "acknowledge"}
        )
    return None


def _rebook_gate(state: dict, inputs: dict) -> dict | None:
    if not state.get("confirmed"):
        raise ValidationFailed("rebook refused: no prior confirm step")
    fid = inputs.get("new_flight_id")
    if fid not in (state.get("options") or []):
        raise ValidationFailed(
            f"rebook refused: flight {fid} was not among the searched options",
            details={"param": "new_flight_id", "options": state.get("options")},
        )
    return None


find._theodosia_validator = _find_gate  # type: ignore[attr-defined]
confirm._theodosia_validator = _confirm_gate  # type: ignore[attr-defined]
rebook._theodosia_validator = _rebook_gate  # type: ignore[attr-defined]


def build_airline_app(*, track: bool = True) -> ApplicationBuilder:
    builder = (
        ApplicationBuilder()
        .with_actions(find=find, search=search, confirm=confirm, rebook=rebook)
        .with_transitions(
            ("find", "search", Condition.expr("stage == 'found'")),
            ("search", "confirm", Condition.expr("stage == 'searched'")),
            ("confirm", "rebook", Condition.expr("stage == 'confirmed'")),
        )
        .with_state(stage="new", confirmed=False)
        .with_entrypoint("find")
    )
    if track:
        builder = builder.with_tracker(tracker(project="comparison-airline"))
    return builder
