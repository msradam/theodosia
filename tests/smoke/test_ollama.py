"""Ollama smoke tests: local models drive theodosia servers in-process.

The server is mounted in-process (FastMCP Client, no subprocess). The model
sees server instructions — which embed the full action surface — as its
system prompt, so no MCP resource reads are needed.

Prerequisite: Ollama reachable at OLLAMA_URL (default http://localhost:11434)
with the target models pulled. For the Mac Mini:

    # On the Mini — start Ollama:
    OLLAMA_HOST=0.0.0.0:11434 ~/ollama-app/Ollama.app/Contents/Resources/ollama serve

    # Locally — tunnel the port:
    ssh -L 11434:localhost:11434 msradam@192.168.1.237 -N

    # Pull the panel:
    ollama pull granite4.1:8b
    ollama pull qwen3:8b
    ollama pull nemotron-3-nano:4b
    ollama pull llama3.1:8b
    ollama pull phi4-mini

Run all panel models:
    uv run pytest -m smoke tests/smoke/test_ollama.py -v -s

Run one model:
    MODEL=phi4-mini uv run pytest -m smoke tests/smoke/test_ollama.py -v -s
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke

_REPO = Path(__file__).resolve().parents[2]
_EXAMPLES = _REPO / "examples"
sys.path.insert(0, str(_EXAMPLES))

from ._helpers import (
    actions_called,
    calls_to,
    calls_with_action,
    check_ollama_or_skip,
    drive_ollama,
    result_for,
)

check_ollama_or_skip()

# Panel of models to run. Override with MODEL env var for a single run.
_DEFAULT_PANEL = [
    "granite4.1:8b",
    "qwen3:8b",
    "nemotron-3-nano:4b",
    "llama3.1:8b",
    "phi4-mini",
    "phi4-mini:latest",
]

_PANEL: list[str] = (
    [_single] if (_single := os.environ.get("MODEL")) else _DEFAULT_PANEL
)


def _available_models() -> set[str]:
    """Models currently pulled in the Ollama instance."""
    import httpx

    url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    try:
        data = httpx.get(f"{url}/api/tags", timeout=5).json()
        return {m["name"] for m in data.get("models", [])}
    except Exception:
        return set()


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "model" in metafunc.fixturenames:
        available = _available_models()
        panel = [m for m in _PANEL if m in available]
        if not panel:
            panel = _PANEL  # let the test skip naturally per model if unavailable
        metafunc.parametrize("model", panel, ids=lambda m: m.replace(":", "_"))


@pytest.fixture()
def skip_if_not_pulled(model: str) -> None:
    available = _available_models()
    if model not in available:
        pytest.skip(f"{model!r} not pulled — run: ollama pull {model}")


@pytest.mark.asyncio
@pytest.mark.usefixtures("skip_if_not_pulled")
async def test_walks_coffee_to_terminal(model: str) -> None:
    """Model places a coffee order and walks the FSM to terminal state.

    Tests: FSM navigation from instructions alone (no resource reads).
    """
    from coffee_order import build_server

    trace = await drive_ollama(
        build_server,
        (
            "Place a coffee order: one latte for $5.50, then pay, then fulfill. "
            "Call the step tool at each stage. Stop when valid_next_actions is empty."
        ),
        model=model,
        max_turns=20,
    )

    step_calls = calls_to(trace["tool_calls"], "step")
    assert step_calls, (
        f"[{model}] Never called step. Tools: {[c['name'] for c in trace['tool_calls']]}"
    )

    actions = actions_called(trace["tool_calls"], "step")
    assert "take_order" in actions, f"[{model}] take_order never called. Actions: {actions}"
    assert "fulfill" in actions, f"[{model}] fulfill never called. Actions: {actions}"

    last_result = result_for(trace["tool_results"], step_calls[-1]["id"])
    assert last_result is not None
    parsed = last_result["parsed"]
    assert parsed is not None
    assert parsed.get("error") is None, f"[{model}] Last step errored: {parsed!r}"
    assert parsed.get("valid_next_actions") == [], (
        f"[{model}] FSM didn't reach terminal: {parsed!r}"
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("skip_if_not_pulled")
async def test_refusal_triggers_self_correction(model: str) -> None:
    """Model self-corrects after an invalid_transition refusal.

    Tests: structured error body is read; valid_next_actions used to recover.
    """
    from coffee_order import build_server

    trace = await drive_ollama(
        build_server,
        (
            "First, call step with action=pay (before placing any order) so we see "
            "the structured refusal. Then use valid_next_actions from the refusal to "
            "recover: call take_order, then pay ($5.50), then fulfill."
        ),
        model=model,
        max_turns=20,
    )

    step_calls = calls_to(trace["tool_calls"], "step")
    assert step_calls, f"[{model}] Never called step."

    pay_calls = calls_with_action(trace["tool_calls"], "step", "pay")
    assert pay_calls, f"[{model}] Skipped the deliberate pay-first call."

    first_pay_result = result_for(trace["tool_results"], pay_calls[0]["id"])
    assert first_pay_result is not None
    parsed = first_pay_result["parsed"]
    assert parsed is not None
    assert parsed.get("error") == "invalid_transition", (
        f"[{model}] Expected invalid_transition; got {parsed!r}"
    )
    assert "take_order" in parsed.get("valid_next_actions", []), (
        f"[{model}] Refusal didn't carry take_order: {parsed!r}"
    )

    later = actions_called(trace["tool_calls"], "step")[1:]
    assert "take_order" in later, f"[{model}] Didn't self-correct. Subsequent: {later!r}"


@pytest.mark.asyncio
@pytest.mark.usefixtures("skip_if_not_pulled")
async def test_action_error_surfaced(model: str) -> None:
    """When an action raises, the model receives is_error=True.

    Tests: MCP isError signal lands correctly; model stops rather than
    hallucinating success.
    """
    from chargen import build_server

    trace = await drive_ollama(
        build_server,
        (
            "Walk: begin (name=Test), choose_race (human), choose_class (fighter). "
            "Then call assign_stats with ALL stats set to 8. "
            "When you receive the error response, stop. Do not retry."
        ),
        model=model,
        max_turns=15,
    )

    assign_calls = calls_with_action(trace["tool_calls"], "step", "assign_stats")
    assert assign_calls, f"[{model}] Never called assign_stats."

    bad_result = result_for(trace["tool_results"], assign_calls[0]["id"])
    assert bad_result is not None
    assert bad_result["is_error"] is True, (
        f"[{model}] Expected is_error=True; got {bad_result['is_error']!r}. Result: {bad_result!r}"
    )
