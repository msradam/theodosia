"""drive_claude against a fake Anthropic client (ISSUE-010/011 coverage).

The driver's contract is mechanical: list tools, translate tool_use blocks
into MCP calls, feed tool_result back, stop on terminal / text-only / cap.
A fake `messages.create` exercises all of it without the network; only the
`anthropic` types are needed (for the ToolUseBlock isinstance narrowing),
so the module skips when the claude extra isn't installed.
"""

from __future__ import annotations

import pytest

anthropic = pytest.importorskip("anthropic")

from anthropic.types import TextBlock, ToolUseBlock
from coffee_order import build_application

from theodosia import ServingMode, drive_claude, mount
from theodosia.drive import _default_anthropic_client, _mcp_tools_to_anthropic


class _FakeResponse:
    def __init__(self, content: list) -> None:
        self.content = content


class _FakeMessages:
    """Replays a scripted list of responses, recording each request."""

    def __init__(self, script: list[_FakeResponse]) -> None:
        self._script = list(script)
        self.requests: list[dict] = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self._script:
            # Past the script: keep asking for the same (refused) action so
            # the cap path can be exercised.
            return _FakeResponse(
                [ToolUseBlock(type="tool_use", id="tu_loop", name="step", input={"action": "pay"})]
            )
        return self._script.pop(0)


class _FakeAnthropic:
    def __init__(self, script: list[_FakeResponse]) -> None:
        self.messages = _FakeMessages(script)


def _tool_use(tu_id: str, action: str, inputs: dict | None = None) -> ToolUseBlock:
    return ToolUseBlock(
        type="tool_use",
        id=tu_id,
        name="step",
        input={"action": action, "inputs": inputs or {}},
    )


@pytest.mark.asyncio
async def test_drive_claude_runs_to_terminal_and_reports_state():
    server = mount(build_application, mode=ServingMode.STEP, name="drive-test")
    fake = _FakeAnthropic(
        [
            _FakeResponse([_tool_use("tu_1", "take_order", {"item": "latte", "qty": 1})]),
            _FakeResponse([_tool_use("tu_2", "pay", {"amount": 5.0})]),
            _FakeResponse([_tool_use("tu_3", "fulfill")]),
        ]
    )
    transcript = await drive_claude(server, fake, prompt="Order a latte.")
    assert transcript["stopped_on"] == "terminal"
    assert [t["action"] for t in transcript["turns"]] == ["take_order", "pay", "fulfill"]
    assert transcript["final_state"]["stage"] == "fulfilled"
    # The system prompt carried the FSM cold-start context.
    assert "## FSM graph" in fake.messages.requests[0]["system"]


@pytest.mark.asyncio
async def test_drive_claude_stops_on_text_only_response():
    server = mount(build_application, mode=ServingMode.STEP, name="drive-test")
    fake = _FakeAnthropic([_FakeResponse([TextBlock(type="text", text="I'm done thinking.")])])
    transcript = await drive_claude(server, fake, prompt="Just talk.")
    assert transcript["stopped_on"] == "text_only"
    assert transcript["turns"] == []


@pytest.mark.asyncio
async def test_drive_claude_hits_turn_cap_on_repeated_refusals():
    server = mount(build_application, mode=ServingMode.STEP, name="drive-test")
    # pay before take_order is refused forever; the driver must stop at the cap.
    fake = _FakeAnthropic([])
    transcript = await drive_claude(server, fake, prompt="Pay first.", max_turns=3)
    assert transcript["stopped_on"] == "cap"
    assert len(transcript["turns"]) == 3
    assert all(t["result"].get("error") for t in transcript["turns"])


@pytest.mark.asyncio
async def test_drive_claude_on_step_callback_streams_turns():
    server = mount(build_application, mode=ServingMode.STEP, name="drive-test")
    fake = _FakeAnthropic(
        [
            _FakeResponse([_tool_use("tu_1", "take_order", {"item": "mocha", "qty": 1})]),
            _FakeResponse([TextBlock(type="text", text="paused")]),
        ]
    )
    seen: list[str] = []

    async def on_step(action: str, result: dict) -> None:
        seen.append(action)

    await drive_claude(server, fake, prompt="One step.", on_step=on_step)
    assert seen == ["take_order"]


def test_mcp_tools_to_anthropic_shape():
    class _T:
        def __init__(self) -> None:
            self.name = "step"
            self.description = "Advance."
            self.inputSchema = {"type": "object"}  # MCP wire field name

    out = _mcp_tools_to_anthropic([_T()])
    assert out == [{"name": "step", "description": "Advance.", "input_schema": {"type": "object"}}]


def test_default_client_requires_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        _default_anthropic_client()


def test_default_client_builds_with_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    client = _default_anthropic_client()
    assert type(client).__name__ == "AsyncAnthropic"
