"""Coverage for branches the 2026-06 audit found untested (ISSUE-010).

Each section targets one module's uncovered deterministic paths: the
tracker() resolution order, _tracker fallbacks and trace reading, Assembly
YAML round-trip errors, CLI target resolution errors, step-log parsing
edge cases, and the sessions diff/watch/logs command surfaces. Synthetic
tracker trees are written under tmp_path, mirroring test_cli_sessions.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from coffee_order import build_application
from typer.testing import CliRunner

import theodosia
from theodosia.assembly import Assembly
from theodosia.cli import app

runner = CliRunner()


# ── theodosia.tracker() resolution order ────────────────────────────────


def test_tracker_explicit_storage_dir_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("THEODOSIA_HOME", str(tmp_path / "env-home"))
    t = theodosia.tracker("proj", storage_dir=str(tmp_path / "explicit"))
    # LocalTrackingClient appends the project name to its storage root.
    assert Path(t.storage_dir).parent.name == "explicit"


def test_tracker_env_var_beats_default(tmp_path, monkeypatch):
    monkeypatch.setenv("THEODOSIA_HOME", str(tmp_path / "env-home"))
    t = theodosia.tracker("proj")
    assert Path(t.storage_dir).parent.name == "env-home"


def test_tracker_defaults_to_dot_theodosia(tmp_path, monkeypatch):
    monkeypatch.delenv("THEODOSIA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    t = theodosia.tracker("proj")
    assert Path(t.storage_dir).parent.name == ".theodosia"


def test_resolve_version_falls_back_when_uninstalled(monkeypatch):
    import importlib.metadata

    def _missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", _missing)
    assert theodosia._resolve_version() == "0+unknown"


# ── _tracker fallbacks and trace reading ────────────────────────────────


def test_tracker_helpers_return_none_without_tracker():
    from burr.core import ApplicationBuilder, State, action

    from theodosia._tracker import _tracker_log_path, _tracker_project

    @action(reads=[], writes=["n"])
    def go(state: State) -> State:
        return state.update(n=1)

    app_obj = (
        ApplicationBuilder()
        .with_actions(go=go)
        .with_transitions()
        .with_state(n=0)
        .with_entrypoint("go")
        .build()
    )
    assert _tracker_project(app_obj) is None
    assert _tracker_log_path(app_obj) is None


def test_read_trace_skips_garbage_and_honors_tail(tmp_path):
    from theodosia._tracker import _read_trace

    log = tmp_path / "log.jsonl"
    rows = [json.dumps({"seq": i}) for i in range(5)]
    log.write_text(rows[0] + "\n\n{garbled\n" + "\n".join(rows[1:]) + "\n")
    entries = _read_trace(log, tail=3)
    assert [e["seq"] for e in entries] == [2, 3, 4]


# ── Assembly YAML serialization errors ──────────────────────────────────


def test_assembly_serializes_module_level_factory(tmp_path):
    asm = Assembly(name="t", workflow=build_application)
    out = tmp_path / "asm.yaml"
    text = asm.to_yaml(out)
    assert "coffee_order:build_application" in text
    assert out.exists() and out.read_text() == text


def test_assembly_rejects_unserializable_callable():
    asm = Assembly(name="t", workflow=lambda: None)
    with pytest.raises(ValueError, match="no resolvable import path"):
        asm.to_yaml()


def test_assembly_rejects_non_callable_workflow():
    asm = Assembly(name="t", workflow=build_application())
    with pytest.raises(ValueError, match="only factory"):
        asm.to_yaml()


# ── CLI target resolution errors (cli/_resolve) ─────────────────────────


def test_resolve_unknown_module_exits_cleanly():
    result = runner.invoke(app, ["doctor", "definitely_missing_module:thing"])
    assert result.exit_code != 0


def test_resolve_module_without_attr_exits_cleanly():
    result = runner.invoke(app, ["doctor", "coffee_order:no_such_attr"])
    assert result.exit_code != 0


def test_doctor_runs_against_module_attr_target():
    result = runner.invoke(app, ["doctor", "coffee_order:build_application"])
    assert result.exit_code == 0, result.output


# ── tracker-log parsing edge cases (cli/_steps) ─────────────────────────


def _begin(seq: int, action: str, ts: str = "2026-05-24T12:00:00.000000") -> dict:
    return {
        "type": "begin_entry",
        "start_time": ts,
        "action": action,
        "inputs": {},
        "sequence_id": seq,
    }


def _end(
    seq: int,
    action: str,
    state: dict,
    ts: str = "2026-05-24T12:00:00.500000",
    exception: str | None = None,
) -> dict:
    return {
        "type": "end_entry",
        "end_time": ts,
        "action": action,
        "exception": exception,
        "state": {"__PRIOR_STEP": action, **state},
        "sequence_id": seq,
    }


def _write_session(home: Path, project: str, app_id: str, entries: list[dict]) -> Path:
    log = home / project / app_id / "log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return log


def test_read_steps_tolerates_blank_and_garbled_lines(tmp_path):
    from theodosia.cli._steps import _read_steps

    log = tmp_path / "log.jsonl"
    lines = [
        json.dumps(_begin(0, "take_order")),
        "",
        "{garbled",
        json.dumps({"type": "begin_entry"}),  # no sequence_id: skipped
        json.dumps(_end(0, "take_order", {"stage": "ordered"})),
    ]
    log.write_text("\n".join(lines) + "\n")
    rows = _read_steps(log)
    assert len(rows) == 1
    assert rows[0].status == "ok"
    assert rows[0].state_summary == {"stage": "ordered"}


def test_read_steps_marks_unfinished_step_running(tmp_path):
    from theodosia.cli._steps import _read_steps

    log = tmp_path / "log.jsonl"
    log.write_text(json.dumps(_begin(0, "take_order")) + "\n")
    rows = _read_steps(log)
    assert rows[0].status == "running"
    assert rows[0].duration_ms is None


def test_read_steps_error_row_summarizes_exception(tmp_path):
    from theodosia.cli._steps import _read_steps

    log = tmp_path / "log.jsonl"
    tb = "Traceback (most recent call last):\n  ...\nValueError: card declined\n"
    entries = [_begin(0, "pay"), _end(0, "pay", {}, exception=tb)]
    log.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    rows = _read_steps(log)
    assert rows[0].status == "error"
    assert "ValueError: card declined" in rows[0].error_summary


def test_duration_ms_handles_unparseable_timestamps():
    from theodosia.cli._steps import _duration_ms

    assert _duration_ms("not-a-time", "also-not") is None


# ── sessions CLI surfaces: diff, watch --once, logs --refusals ──────────


def _seed_two_sessions(home: Path) -> tuple[str, str]:
    _write_session(
        home,
        "demo",
        "aaaa1111",
        [
            _begin(0, "take_order"),
            _end(0, "take_order", {"stage": "ordered", "item": "latte"}),
            _begin(1, "pay", ts="2026-05-24T12:00:01.000000"),
            _end(1, "pay", {"stage": "paid", "item": "latte"}, ts="2026-05-24T12:00:01.200000"),
        ],
    )
    _write_session(
        home,
        "demo",
        "bbbb2222",
        [
            _begin(0, "take_order"),
            _end(0, "take_order", {"stage": "ordered", "item": "mocha"}),
            _begin(1, "cancel", ts="2026-05-24T12:00:01.000000"),
            _end(1, "cancel", {"stage": "cancelled"}, ts="2026-05-24T12:00:01.100000"),
        ],
    )
    return "aaaa1111", "bbbb2222"


def test_sessions_diff_renders_divergence(tmp_path):
    a, b = _seed_two_sessions(tmp_path)
    result = runner.invoke(app, ["sessions", "diff", a, b, "--home", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "common prefix" in result.output
    assert "stage" in result.output


def test_sessions_diff_json_shape(tmp_path):
    a, b = _seed_two_sessions(tmp_path)
    result = runner.invoke(app, ["sessions", "diff", a, b, "--home", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["a"]["actions"] == ["take_order", "pay"]
    assert payload["b"]["actions"] == ["take_order", "cancel"]
    assert {c["key"] for c in payload["state_diff"]["changed"]} >= {"stage"}


def test_sessions_diff_identical_session_reports_identity(tmp_path):
    a, _ = _seed_two_sessions(tmp_path)
    result = runner.invoke(app, ["sessions", "diff", a, a, "--home", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "identical" in result.output


def test_watch_once_prints_snapshot(tmp_path):
    a, _ = _seed_two_sessions(tmp_path)
    result = runner.invoke(app, ["watch", a, "--home", str(tmp_path), "--once"])
    assert result.exit_code == 0, result.output
    assert "take_order" in result.output


def test_watch_missing_home_exits_one(tmp_path):
    result = runner.invoke(app, ["watch", "--home", str(tmp_path / "nope")])
    assert result.exit_code == 1


def test_watch_list_aliases_sessions_ls(tmp_path):
    _seed_two_sessions(tmp_path)
    result = runner.invoke(app, ["watch", "--list", "--home", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "demo" in result.output


def test_logs_refusals_only_includes_sidecar(tmp_path):
    a, _ = _seed_two_sessions(tmp_path)
    sidecar = tmp_path / "demo" / a / "refusals.jsonl"
    sidecar.write_text(
        json.dumps(
            {
                "ts": "2026-05-24T12:00:02.000000",
                "action": "fulfill",
                "error": "invalid_transition",
                "reason": "pay first",
            }
        )
        + "\n"
    )
    result = runner.invoke(app, ["logs", a, "--home", str(tmp_path), "--refusals", "--plain"])
    assert result.exit_code == 0, result.output
    assert "fulfill" in result.output


def test_logs_plain_renders_one_line_per_step(tmp_path):
    a, _ = _seed_two_sessions(tmp_path)
    result = runner.invoke(app, ["logs", a, "--home", str(tmp_path), "--plain"])
    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    assert len(lines) == 2
    assert "OK" in lines[0]


# ── `theodosia verify`: ledger attestation CLI (cli/status) ─────────────


def _seed_ledger(home: Path, project: str = "demo", app_id: str = "cccc3333") -> Path:
    from theodosia.ledger import HashChainedLedger

    _write_session(home, project, app_id, [_begin(0, "take_order")])
    ledger_path = home / project / app_id / "ledger.jsonl"
    led = HashChainedLedger(ledger_path)
    led.append({"seq": 0, "action": "take_order"})
    led.append({"seq": 1, "action": "pay"})
    return ledger_path


def test_verify_intact_ledger_passes(tmp_path):
    _seed_ledger(tmp_path)
    result = runner.invoke(app, ["verify", "cccc3333", "--home", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "intact" in result.output


def test_verify_json_receipt_shape(tmp_path):
    _seed_ledger(tmp_path)
    result = runner.invoke(app, ["verify", "cccc3333", "--home", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert receipt["ok"] is True
    assert receipt["entries"] == 2
    assert receipt["head_hash"].startswith("sha256:")


def test_verify_tampered_ledger_fails(tmp_path):
    ledger_path = _seed_ledger(tmp_path)
    lines = ledger_path.read_text().splitlines()
    entry = json.loads(lines[0])
    entry["action"] = "forged"
    lines[0] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    ledger_path.write_text("\n".join(lines) + "\n")
    result = runner.invoke(app, ["verify", "cccc3333", "--home", str(tmp_path)])
    assert result.exit_code == 1
    assert "TAMPERED" in result.output


def test_verify_missing_ledger_exits_one(tmp_path):
    _write_session(tmp_path, "demo", "dddd4444", [_begin(0, "take_order")])
    result = runner.invoke(app, ["verify", "dddd4444", "--home", str(tmp_path)])
    assert result.exit_code == 1
    assert "No ledger" in result.output


# ── app-id / project resolution errors (cli/_resolve) ───────────────────


def test_unknown_app_id_prefix_bails(tmp_path):
    _seed_two_sessions(tmp_path)
    result = runner.invoke(app, ["logs", "ffff9999", "--home", str(tmp_path)])
    assert result.exit_code != 0


def test_ambiguous_app_id_prefix_lists_matches(tmp_path):
    _write_session(tmp_path, "demo", "abc11111", [_begin(0, "a"), _end(0, "a", {})])
    _write_session(tmp_path, "demo", "abc22222", [_begin(0, "a"), _end(0, "a", {})])
    result = runner.invoke(app, ["logs", "abc", "--home", str(tmp_path)])
    assert result.exit_code != 0
    assert "ambiguous" in result.output


def test_empty_home_bails_with_message(tmp_path):
    (tmp_path / "empty").mkdir()
    result = runner.invoke(app, ["logs", "--home", str(tmp_path / "empty")])
    assert result.exit_code != 0


def test_theodosia_home_env_resolves_sessions(tmp_path, monkeypatch):
    a, _ = _seed_two_sessions(tmp_path)
    monkeypatch.setenv("THEODOSIA_HOME", str(tmp_path))
    result = runner.invoke(app, ["logs", a, "--plain"])
    assert result.exit_code == 0, result.output


# ── `theodosia render`: static + dot topology (cli/_topology) ───────────


def test_render_ascii_topology():
    result = runner.invoke(app, ["render", "coffee_order:build_application"])
    assert result.exit_code == 0, result.output
    assert "take_order" in result.output


def test_render_dot_output():
    result = runner.invoke(app, ["render", "coffee_order:build_application", "--dot"])
    assert result.exit_code == 0, result.output
    assert "digraph" in result.output


def test_render_annotated_with_session(tmp_path):
    a, _ = _seed_two_sessions(tmp_path)
    result = runner.invoke(
        app,
        ["render", "coffee_order:build_application", "--app-id", a, "--home", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output


# ── step-row helper branches (cli/_steps) ───────────────────────────────


def test_post_state_scans_forward_for_sync_action_staleness(tmp_path):
    """Sync actions record pre-step state; the reader scans forward (ISSUE-010)."""
    from theodosia.cli._steps import _read_steps

    log = tmp_path / "log.jsonl"
    entries = [
        _begin(0, "take_order"),
        # Stale end: state still names the previous action.
        {
            "type": "end_entry",
            "end_time": "2026-05-24T12:00:00.500000",
            "action": "take_order",
            "exception": None,
            "state": {"__PRIOR_STEP": "__init__", "stage": "new"},
            "sequence_id": 0,
        },
        _begin(1, "pay", ts="2026-05-24T12:00:01.000000"),
        # The forward entry carries take_order's true post-state.
        {
            "type": "end_entry",
            "end_time": "2026-05-24T12:00:01.500000",
            "action": "pay",
            "exception": None,
            "state": {"__PRIOR_STEP": "take_order", "stage": "ordered"},
            "sequence_id": 1,
        },
    ]
    log.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    rows = _read_steps(log)
    assert rows[0].state_summary == {"stage": "ordered"}


def test_short_value_truncates_and_summarizes():
    from theodosia.cli._steps import _short_value

    assert _short_value("x" * 40).endswith("…")
    assert _short_value([1, 2, 3]) == "[3 items]"
    assert _short_value({"a": 1}) == "{1 keys}"
    assert _short_value(42) == "42"


def test_state_diff_text_branches():
    from theodosia.cli._steps import _state_diff_text

    assert _state_diff_text({"a": 1}, {"a": 1}) == "(no state change)"
    assert "b=2" in _state_diff_text({"a": 1, "b": 2}, {"a": 1})
    first = _state_diff_text({"a": 0, "b": "", "c": "x"}, None)
    assert first == "c=x"  # falsy values hidden on the first row


def test_relative_when_buckets():
    from datetime import datetime, timedelta

    from theodosia.cli._steps import _relative_when

    now = datetime.now()
    assert _relative_when("") == ""
    assert _relative_when("not-a-ts") == "not-a-ts"
    assert _relative_when((now - timedelta(seconds=30)).isoformat()).endswith("s ago")
    assert _relative_when((now - timedelta(minutes=5)).isoformat()).endswith("m ago")
    assert _relative_when((now - timedelta(hours=3)).isoformat()).endswith("h ago")
    assert _relative_when((now - timedelta(days=2)).isoformat()).endswith("d ago")


# ── drive: resource formatting fallbacks ────────────────────────────────


@pytest.mark.asyncio
async def test_format_resource_swallows_errors_and_empties():
    from theodosia.drive import _format_resource

    class _Boom:
        async def read_resource(self, uri):
            raise RuntimeError("nope")

    class _Empty:
        async def read_resource(self, uri):
            return []

    assert await _format_resource(_Boom(), "theodosia://state") == ""
    assert await _format_resource(_Empty(), "theodosia://state") == ""


# ── run(): exit-code handling (cli/_app) ────────────────────────────────


def test_run_returns_two_for_unknown_command():
    from theodosia.cli import run

    assert run(app, ["bogus-command"]) == 2


def test_run_returns_two_for_unknown_option():
    from theodosia.cli import run

    assert run(app, ["sessions", "ls", "--no-such-flag"]) == 2


def test_run_returns_zero_for_help():
    from theodosia.cli import run

    assert run(app, ["--help"]) == 0


# ── sessions ls / show surfaces ─────────────────────────────────────────


def test_sessions_ls_json_lists_projects(tmp_path):
    _seed_two_sessions(tmp_path)
    result = runner.invoke(app, ["sessions", "ls", "--home", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload[0]["project"] == "demo"
    assert len(payload[0]["apps"]) == 2


def test_sessions_ls_unknown_project_exits_one(tmp_path):
    _seed_two_sessions(tmp_path)
    result = runner.invoke(app, ["sessions", "ls", "--home", str(tmp_path), "--project", "nope"])
    assert result.exit_code == 1


def test_sessions_show_renders_table_and_json(tmp_path):
    a, _ = _seed_two_sessions(tmp_path)
    result = runner.invoke(app, ["sessions", "show", a, "--home", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "take_order" in result.output
    result = runner.invoke(app, ["sessions", "show", a, "--home", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [r["action"] for r in payload["steps"]] == ["take_order", "pay"]


# ── _resolve_home: ~/.burr fallbacks (cli/_resolve) ─────────────────────


def test_resolve_home_falls_back_to_dot_burr(tmp_path, monkeypatch):
    # conftest isolates HOME to tmp_path; populate only ~/.burr.
    monkeypatch.delenv("THEODOSIA_HOME", raising=False)
    burr_home = tmp_path / ".burr"
    _write_session(burr_home, "burrproj", "eeee5555", [_begin(0, "a"), _end(0, "a", {})])
    from theodosia.cli._resolve import _resolve_home

    assert _resolve_home(None) == burr_home


def test_locate_project_home_prefers_burr_when_project_only_there(tmp_path, monkeypatch):
    monkeypatch.delenv("THEODOSIA_HOME", raising=False)
    theo_home = tmp_path / ".theodosia"
    _write_session(theo_home, "other", "aaaa0000", [_begin(0, "a"), _end(0, "a", {})])
    burr_home = tmp_path / ".burr"
    _write_session(burr_home, "burronly", "bbbb0000", [_begin(0, "a"), _end(0, "a", {})])
    from theodosia.cli.sessions import _locate_project_home

    assert _locate_project_home(None, "burronly") == burr_home
    assert _locate_project_home(None, "other") == theo_home


# ── refusal sidecar parsing edge cases (cli/_steps) ─────────────────────


def test_read_refusals_skips_blank_and_garbled_lines(tmp_path):
    from theodosia.cli._steps import _read_refusals

    log = tmp_path / "log.jsonl"
    log.write_text("")
    sidecar = tmp_path / "refusals.jsonl"
    sidecar.write_text(
        "\n{garbled\n"
        + json.dumps({"ts": "2026-05-24T12:00:00", "action": "pay", "error": "invalid"})
        + "\n"
    )
    rows = _read_refusals(log)
    assert len(rows) == 1
    assert rows[0].action == "pay"
    assert rows[0].status == "error"


# ── final _steps micro-branches + report command ────────────────────────


def test_terminal_state_staleness_heuristic():
    from theodosia.cli._steps import StepRow, _terminal_state_may_be_stale

    def row(action: str, prior: str | None) -> StepRow:
        return StepRow(
            seq=0,
            action=action,
            started="",
            duration_ms=None,
            status="ok",
            error_summary=None,
            state_summary={},
            state_raw=None if prior is None else {"__PRIOR_STEP": prior},
        )

    assert _terminal_state_may_be_stale([]) is False
    assert _terminal_state_may_be_stale([row("pay", None)]) is False
    assert _terminal_state_may_be_stale([row("pay", "pay")]) is False
    assert _terminal_state_may_be_stale([row("pay", "take_order")]) is True


def test_exception_summary_fallbacks():
    from theodosia.cli._steps import _exception_summary

    assert _exception_summary("") == "exception"
    assert _exception_summary("just a stray line\n)") == ")"
    assert _exception_summary("noise\nValueError: boom\n)").startswith("ValueError: boom")


def test_relative_when_future_timestamp_shows_clock_time():
    from datetime import datetime, timedelta

    from theodosia.cli._steps import _relative_when

    future = (datetime.now() + timedelta(hours=1)).isoformat()
    out = _relative_when(future)
    assert "ago" not in out


def test_status_text_empty_glyph():
    from theodosia.cli._steps import _status_text

    assert _status_text("empty").plain == "∅"
    assert _status_text("running").plain == "•"


def test_sessions_ls_all_includes_unadvanced(tmp_path):
    _seed_two_sessions(tmp_path)
    empty = tmp_path / "demo" / "cccc0000"
    empty.mkdir(parents=True)
    hidden = runner.invoke(app, ["sessions", "ls", "--home", str(tmp_path), "--json"])
    shown = runner.invoke(app, ["sessions", "ls", "--home", str(tmp_path), "--json", "--all"])
    assert len(json.loads(hidden.output)[0]["apps"]) == 2
    assert len(json.loads(shown.output)[0]["apps"]) == 3


def test_report_writes_markdown_file(tmp_path):
    a, _ = _seed_two_sessions(tmp_path)
    out = tmp_path / "report.md"
    result = runner.invoke(app, ["report", a, "--home", str(tmp_path), "--out", str(out)])
    assert result.exit_code == 0, result.output
    text = out.read_text()
    assert "## Timeline" in text
    assert "take_order" in text
