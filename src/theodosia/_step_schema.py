"""Pydantic models + JSON Schema for the ``step`` tool's response.

The Pydantic models declare the contract clients see in ``step``'s output
schema; the body of ``step`` returns plain dicts shaped to match these.
The schema function emits the merged-object JSON Schema MCP expects (one
top-level ``type: "object"`` with the ``error`` field as the
discriminator), since MCP does not accept a union at the top level.

This module is the wire contract; everything else in the adapter shapes
data to land in one of these shapes.
"""

from __future__ import annotations

from typing import Any


def _step_response_schema() -> dict[str, Any]:
    """JSON Schema for the ``step`` tool's response.

    MCP requires the output schema to be a single ``type: "object"`` at
    the top level, not a union. We emit a merged object whose ``error``
    field discriminates: when ``error`` is absent the response carries
    the ``_StepSuccess`` fields (action, result, state, ...); when
    ``error`` is present it carries one of the refusal shapes
    enumerated in ``error``'s allowed values. Per-shape required-field
    constraints are documented in ``description`` rather than enforced
    via ``oneOf``; clients should branch on ``error`` to interpret the
    rest of the payload.
    """
    return {
        "type": "object",
        "description": (
            "Result of one step. When `error` is absent, the response is a "
            "successful step (action, result, state, valid_next_actions, "
            "app_id, tracker_project, optionally streamed/chunks). When "
            "`error` is set, the response is a structured refusal; check "
            "`error` to interpret the rest:\n"
            "  - unknown_action: requested + known_actions + valid_next_actions + message\n"
            "  - invalid_transition: requested + valid_next_actions + message\n"
            "  - validation_failed: requested + reason + details + valid_next_actions\n"
            "  - action_timeout: requested + timeout_seconds + message + valid_next_actions\n"
            "  - action_error: requested + error_type + error_message + valid_next_actions"
        ),
        "properties": {
            # Success-side fields.
            "action": {"type": "string", "description": "Name of the action that ran."},
            "result": {
                "type": ["object", "null"],
                "additionalProperties": True,
                "description": "Action's structured return value.",
            },
            "state": {
                "type": "object",
                "additionalProperties": True,
                "description": "Public Application state after the step.",
            },
            "app_id": {"type": "string", "description": "Application uid."},
            "session": {
                "type": ["string", "null"],
                "description": (
                    "Session handle. Pass this back as step's `session` "
                    "argument to continue the same session on transports "
                    "without protocol-level sessions."
                ),
            },
            "tracker_project": {
                "type": ["string", "null"],
                "description": "LocalTrackingClient project name if attached.",
            },
            "streamed": {
                "type": ["boolean", "null"],
                "description": "True when the action was a streaming action.",
            },
            "chunks": {
                "type": ["integer", "null"],
                "description": "Streamed chunk count (when streamed is true).",
            },
            # Refusal-side fields.
            "error": {
                "type": "string",
                "enum": [
                    "unknown_action",
                    "invalid_transition",
                    "validation_failed",
                    "action_timeout",
                    "action_error",
                ],
                "description": (
                    "Refusal discriminator. When set, the response carries the "
                    "matching refusal payload's fields."
                ),
            },
            "requested": {
                "type": "string",
                "description": "Name the client passed; present on every refusal.",
            },
            "known_actions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "All action names in the FSM (unknown_action only).",
            },
            "valid_next_actions": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Actions reachable from the current state. Present on "
                    "success and on every refusal so the agent can self-correct."
                ),
            },
            "message": {
                "type": "string",
                "description": (
                    "Human-readable message (unknown_action, invalid_transition, action_timeout)."
                ),
            },
            "next_hint": {
                "type": "string",
                "description": (
                    "Directional steering string appended after every step and "
                    "refusal: cites what just happened and the reachable actions "
                    "now. Present on success and on every refusal."
                ),
            },
            "next_action_schemas": {
                "type": "object",
                "additionalProperties": {"type": "object", "additionalProperties": True},
                "description": (
                    "Input schema for each action in valid_next_actions, keyed by "
                    "action name. Each value maps a parameter name to its type "
                    "(and default, when present), so the agent learns how to call "
                    "an action exactly when it becomes reachable. Present on success "
                    "and on every refusal that carries valid_next_actions."
                ),
            },
            "reason": {
                "type": "string",
                "description": "Validation failure reason (validation_failed only).",
            },
            "details": {
                "type": ["object", "null"],
                "additionalProperties": True,
                "description": "Validation failure details (validation_failed only).",
            },
            "timeout_seconds": {
                "type": "number",
                "description": "Configured timeout (action_timeout only).",
            },
            "next_external_tools": {
                "type": "object",
                "additionalProperties": {"type": "array", "items": {"type": "string"}},
                "description": (
                    "Present only when the server was mounted with external_tools. "
                    "Maps each currently-reachable action to the tools (on other "
                    "connected MCP servers) relevant before taking it. Call those "
                    "tools, then step() to record findings and advance."
                ),
            },
            "error_type": {
                "type": "string",
                "description": (
                    "Exception class name of the underlying error (action_error only)."
                ),
            },
            "error_message": {
                "type": "string",
                "description": "Stringified exception (action_error only).",
            },
        },
        "additionalProperties": True,
    }
