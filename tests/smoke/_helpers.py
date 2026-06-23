"""Shared helpers for the smoke suite.

Centralises the environment probe (claude on PATH, OAuth set up,
.mcp.json present, SDK installed) and the ``_drive`` harness that
sends one prompt through the Agent SDK and collects the structured
tool-call trace.

Entry points:

* ``check_environment_or_skip`` — requires the full demo bench
  (~/theodosia-demo/.mcp.json + running servers + Claude auth).
* ``check_cli_or_skip`` — requires only the ``claude`` CLI and OAuth;
  used by the in-process tests that mount theodosia servers directly.
* ``check_ollama_or_skip`` — requires Ollama running at OLLAMA_URL.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

MCP_CONFIG = Path("~/theodosia-demo/.mcp.json").expanduser()


def _check_claude_auth() -> None:
    """Shared auth check; raises pytest.skip if not ready."""
    if shutil.which("claude") is None:
        pytest.skip("Smoke tests require the `claude` CLI on PATH.", allow_module_level=True)

    try:
        proc = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        info = json.loads(proc.stdout or "{}")
        logged_in = bool(info.get("loggedIn"))
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
        logged_in = False

    if not logged_in:
        pytest.skip(
            "Smoke tests require Claude OAuth. Run `claude auth login` "
            "once, then re-run `pytest -m smoke`.",
            allow_module_level=True,
        )

    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        pytest.skip("claude-agent-sdk not installed", allow_module_level=True)


def check_environment_or_skip() -> None:
    """Module-level skip predicate for the demo-bench tests.

    Requires the ``claude`` CLI, Claude OAuth, ``claude-agent-sdk``, AND
    the demo bench config at ``~/theodosia-demo/.mcp.json``.
    """
    if not MCP_CONFIG.exists():
        pytest.skip(
            "Smoke tests require ~/theodosia-demo/.mcp.json. Run `claude auth login` first.",
            allow_module_level=True,
        )
    _check_claude_auth()


def check_cli_or_skip() -> None:
    """Module-level skip predicate for in-process smoke tests.

    Requires only the ``claude`` CLI and OAuth. No external server config.
    """
    _check_claude_auth()


async def drive(
    prompt: str,
    *,
    max_budget_usd: float = 5.0,
    max_turns: int = 30,
) -> dict[str, Any]:
    """Send ``prompt`` through Claude with the demo bench wired in.

    Returns a dict with:
      - tool_calls: list of {"id", "name", "input"} for every tool the
        model invoked.
      - tool_results: list of {"tool_use_id", "content", "is_error",
        "parsed"} where ``parsed`` is the JSON payload Theodosia returned.
      - final_text: concatenated text from assistant messages.
      - result: the ResultMessage (cost, error info, etc.).
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
        query,
    )

    options = ClaudeAgentOptions(
        mcp_servers=MCP_CONFIG,
        allowed_tools=["mcp__*"],
        permission_mode="bypassPermissions",
        max_budget_usd=max_budget_usd,
        max_turns=max_turns,
    )

    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    final_text_parts: list[str] = []
    result_message: ResultMessage | None = None

    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    tool_calls.append(
                        {"id": block.id, "name": block.name, "input": dict(block.input)}
                    )
                elif isinstance(block, TextBlock):
                    final_text_parts.append(block.text)
        elif isinstance(msg, UserMessage):
            content = msg.content
            if isinstance(content, list):
                tool_results.extend(
                    {
                        "tool_use_id": block.tool_use_id,
                        "content": block.content,
                        "is_error": block.is_error,
                        "parsed": parse_tool_result(block.content),
                    }
                    for block in content
                    if isinstance(block, ToolResultBlock)
                )
        elif isinstance(msg, ResultMessage):
            result_message = msg

    return {
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "final_text": "\n".join(final_text_parts),
        "result": result_message,
    }


async def drive_inprocess(
    launch_cmd: list[str],
    server_name: str,
    prompt: str,
    *,
    model: str | None = None,
    max_budget_usd: float = 5.0,
    max_turns: int = 30,
) -> dict[str, Any]:
    """Drive a theodosia server via stdio subprocess through a Claude session.

    ``launch_cmd`` is the command (passed as ``[executable, *args]``) that
    starts the server with stdio transport. ``server_name`` becomes the MCP
    server name and tool prefix (``mcp__<name>__step``). No external config
    file needed — the SDK spawns the server and talks to it over stdio.

    Returns the same shape as ``drive()``.
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
        query,
    )

    options = ClaudeAgentOptions(
        mcp_servers={
            server_name: {
                "type": "stdio",
                "command": launch_cmd[0],
                "args": launch_cmd[1:],
            }
        },
        allowed_tools=[f"mcp__{server_name}__*"],
        permission_mode="bypassPermissions",
        model=model,
        max_budget_usd=max_budget_usd,
        max_turns=max_turns,
    )

    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    final_text_parts: list[str] = []
    result_message: ResultMessage | None = None

    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    tool_calls.append(
                        {"id": block.id, "name": block.name, "input": dict(block.input)}
                    )
                elif isinstance(block, TextBlock):
                    final_text_parts.append(block.text)
        elif isinstance(msg, UserMessage):
            content = msg.content
            if isinstance(content, list):
                tool_results.extend(
                    {
                        "tool_use_id": block.tool_use_id,
                        "content": block.content,
                        "is_error": block.is_error,
                        "parsed": parse_tool_result(block.content),
                    }
                    for block in content
                    if isinstance(block, ToolResultBlock)
                )
        elif isinstance(msg, ResultMessage):
            result_message = msg

    return {
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "final_text": "\n".join(final_text_parts),
        "result": result_message,
    }


def parse_tool_result(content: Any) -> dict | None:
    """Extract the JSON payload from a tool result.

    Theodosia returns two content blocks: a headline and a JSON body.
    Handles both the Claude Agent SDK format (list of dicts) and the
    in-process FastMCP path (concatenated text string).
    """
    if content is None:
        return None
    text: str | None = None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        # Try each text block in reverse — theodosia puts JSON body last.
        texts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        for t in reversed(texts):
            if t:
                try:
                    return json.loads(t)
                except (json.JSONDecodeError, TypeError):
                    if text is None:
                        text = t
        if text is None:
            return None

    if text is None:
        return None

    # Multi-line: "headline\n{json}" — scan lines in reverse for JSON body.
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith(("{", "[")):
            try:
                return json.loads(stripped)
            except (json.JSONDecodeError, TypeError):
                continue
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"_raw": text}


def calls_to(tool_calls: list[dict[str, Any]], tool_name: str) -> list[dict[str, Any]]:
    return [c for c in tool_calls if c["name"] == tool_name]


def calls_with_action(
    tool_calls: list[dict[str, Any]], tool_name: str, action: str
) -> list[dict[str, Any]]:
    """Filter step-tool calls by action name."""
    return [c for c in tool_calls if c["name"] == tool_name and c["input"].get("action") == action]


def actions_called(tool_calls: list[dict[str, Any]], tool_name: str) -> list[str]:
    return [
        c["input"].get("action")
        for c in tool_calls
        if c["name"] == tool_name and c["input"].get("action") is not None
    ]


def result_for(tool_results: list[dict[str, Any]], tool_use_id: str) -> dict[str, Any] | None:
    for r in tool_results:
        if r["tool_use_id"] == tool_use_id:
            return r
    return None


# ---------------------------------------------------------------------------
# Ollama helpers
# ---------------------------------------------------------------------------

_DEFAULT_OLLAMA_URL = "http://localhost:11434"


def check_ollama_or_skip(base_url: str | None = None) -> None:
    """Module-level skip predicate for Ollama-backed smoke tests.

    Checks that Ollama is reachable at ``base_url`` (or the ``OLLAMA_URL``
    env var, defaulting to ``http://localhost:11434``) and that the
    ``openai`` package is installed.
    """
    url = base_url or os.environ.get("OLLAMA_URL", _DEFAULT_OLLAMA_URL)
    try:
        import httpx

        resp = httpx.get(f"{url}/api/tags", timeout=5)
        resp.raise_for_status()
    except Exception as exc:
        pytest.skip(
            f"Ollama not reachable at {url} ({exc}). Start it or set OLLAMA_URL and retry.",
            allow_module_level=True,
        )
    try:
        import openai  # noqa: F401
    except ImportError:
        pytest.skip("openai package not installed", allow_module_level=True)


def _mcp_tools_to_openai(tools: list[Any]) -> list[dict[str, Any]]:
    """Convert FastMCP Tool objects to the OpenAI function-calling schema."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.inputSchema or {"type": "object", "properties": {}},
            },
        }
        for t in tools
    ]


def _mcp_result_text(result: Any) -> str:
    """Extract concatenated text from a FastMCP CallToolResult."""
    parts = []
    for item in result.content or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


async def drive_ollama(
    build_fn: Any,
    prompt: str,
    *,
    model: str = "granite4.1:8b",
    base_url: str | None = None,
    max_turns: int = 30,
) -> dict[str, Any]:
    """Drive a theodosia server in-process using a local Ollama model.

    ``build_fn`` is a callable that returns a FastMCP server (e.g. the
    example modules' ``build_server()``). The server instructions (which
    include the full action surface) are passed as the system prompt so
    the model doesn't need to read any MCP resources.

    Returns the same trace shape as ``drive_inprocess``:
      - tool_calls: list of {"id", "name", "input"}
      - tool_results: list of {"tool_use_id", "content", "is_error", "parsed"}
      - final_text: concatenated assistant text
      - result: None (no ResultMessage for local models)
    """
    from fastmcp import Client
    from openai import AsyncOpenAI

    ollama_url = base_url or os.environ.get("OLLAMA_URL", _DEFAULT_OLLAMA_URL)
    server = build_fn()
    instructions = getattr(server, "instructions", None) or ""

    async with Client(server) as mcp_client:
        mcp_tools = await mcp_client.list_tools()
        openai_tools = _mcp_tools_to_openai(mcp_tools)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": instructions},
            {"role": "user", "content": prompt},
        ]

        ollama = AsyncOpenAI(base_url=f"{ollama_url}/v1", api_key="ollama")

        tool_calls_trace: list[dict[str, Any]] = []
        tool_results_trace: list[dict[str, Any]] = []
        final_text_parts: list[str] = []

        for _ in range(max_turns):
            response = await ollama.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                tools=openai_tools,  # type: ignore[arg-type]
                timeout=120,
            )
            msg = response.choices[0].message
            if msg.content:
                final_text_parts.append(msg.content)

            if not msg.tool_calls:
                break

            # Add assistant turn (with tool_calls) to history.
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
                }
            )

            # Execute each MCP tool call and collect results.
            for tc in msg.tool_calls:
                try:
                    raw_args = tc.function.arguments
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except (json.JSONDecodeError, TypeError):
                    args = {}
                tool_calls_trace.append({"id": tc.id, "name": tc.function.name, "input": args})

                result = await mcp_client.call_tool(tc.function.name, args, raise_on_error=False)
                result_text = _mcp_result_text(result)
                is_error = bool(getattr(result, "is_error", False))

                tool_results_trace.append(
                    {
                        "tool_use_id": tc.id,
                        "content": result_text,
                        "is_error": is_error,
                        "parsed": parse_tool_result(result_text),
                    }
                )
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_text})

        return {
            "tool_calls": tool_calls_trace,
            "tool_results": tool_results_trace,
            "final_text": "\n".join(final_text_parts),
            "result": None,
        }
