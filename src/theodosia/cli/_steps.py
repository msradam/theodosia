"""Reading and rendering Burr tracker log rows (the shared model + table builders)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.table import Table
from rich.text import Text


@dataclass
class StepRow:
    seq: int
    action: str
    started: str
    duration_ms: float | None
    status: str  # "ok" | "error" | "running"
    error_summary: str | None
    state_summary: dict[str, Any]
    state_raw: dict[str, Any] | None = None  # full state dict including __PRIOR_STEP
    # Which reset-delimited run of the session this row belongs to. Burr
    # restarts sequence_id at 0 after reset_session; the epoch keeps both
    # runs in the timeline instead of letting the restart shadow the first.
    # -1 marks rows (refusal sidecar) whose epoch is unknown.
    epoch: int = 0


def _read_refusals(log_path: Path) -> list[StepRow]:
    """Blocked transitions the agent attempted.

    Burr's tracker never sees them because the action body never ran. The
    adapter writes them to a
    ``refusals.jsonl`` sidecar so the postmortem timeline still shows them.
    """
    sidecar = log_path.parent / "refusals.jsonl"
    if not sidecar.exists():
        return []
    rows: list[StepRow] = []
    with sidecar.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            reason = rec.get("refusal_reason") or "refused"
            msg = rec.get("error_message")
            rows.append(
                StepRow(
                    seq=rec.get("seq", -1),
                    action=rec.get("action", "?"),
                    started=rec.get("ts", ""),
                    duration_ms=None,
                    status="error",
                    error_summary=f"{reason}: {msg}" if msg else reason,
                    state_summary={},
                    epoch=-1,
                )
            )
    return rows


def _terminal_state_may_be_stale(steps: list[StepRow]) -> bool:
    """Heuristic for the Burr astep+sync-action staleness on the terminal row.

    Burr records pre-step state in ``end_entry`` when the action body is sync.
    For non-terminal rows the CLI scans forward; the terminal row has no
    forward entry, so its state stays stale if the body was sync. Detect by
    checking whether ``__PRIOR_STEP`` in the row's snapshot names the row's
    action (true post-state) or the previous action (stale pre-state).
    """
    if not steps:
        return False
    last = steps[-1]
    raw = getattr(last, "state_raw", None)
    if raw is None:
        return False
    return bool(raw.get("__PRIOR_STEP") != last.action)


def _scan_tracker_log(
    log_path: Path,
) -> tuple[dict[tuple[int, int], dict[str, Any]], dict[tuple[int, int], dict[str, Any]]]:
    """Read a Burr tracker JSONL into ``(begins, ends)`` keyed by (epoch, seq).

    ``reset_session`` rebuilds the Application, and Burr restarts
    ``sequence_id`` at 0 in the same ``log.jsonl``. Keyed by seq alone, the
    post-reset entries would overwrite the pre-reset ones and the timeline
    would silently understate what happened. A ``begin_entry`` whose seq is
    already present in the current epoch marks the start of a new epoch.
    """
    begins: dict[tuple[int, int], dict[str, Any]] = {}
    ends: dict[tuple[int, int], dict[str, Any]] = {}
    if not log_path.exists():
        return begins, ends
    epoch = 0
    with log_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            seq = rec.get("sequence_id")
            if seq is None:
                continue
            if rec.get("type") == "begin_entry":
                if (epoch, seq) in begins:
                    epoch += 1
                begins[(epoch, seq)] = rec
            elif rec.get("type") == "end_entry":
                ends[(epoch, seq)] = rec
    return begins, ends


def _completed_row(
    seq: int,
    b: dict[str, Any],
    e: dict[str, Any],
    state: dict[str, Any],
    epoch: int = 0,
) -> StepRow:
    """Build the row for a step with both begin and end entries."""
    started = b.get("start_time", "")
    exc = e.get("exception")
    state_view = {k: v for k, v in state.items() if not k.startswith("__")}
    return StepRow(
        seq=seq,
        action=b.get("action", "?"),
        started=started,
        duration_ms=_duration_ms(started, e.get("end_time", "")),
        status="error" if exc else "ok",
        error_summary=_exception_summary(str(exc))[:140] if exc else None,
        state_summary=state_view,
        state_raw=state,
        epoch=epoch,
    )


def _read_steps(log_path: Path) -> list[StepRow]:
    """Pair begin/end entries from a Burr tracker JSONL into rows.

    Works around Burr's sync-action staleness: when an ``@action`` body is
    sync, ``post_run_step`` (the source of ``end_entry``) fires with
    pre-step state. We detect this per-row by checking ``__PRIOR_STEP`` in
    the recorded state. If it does not match the row's action, we scan
    forward for the entry whose ``__PRIOR_STEP`` does match. That entry's
    state is the true post-step state for this row.
    """
    begins, ends = _scan_tracker_log(log_path)

    def _post_state(key: tuple[int, int], action_name: str) -> dict[str, Any]:
        e = ends.get(key)
        if e is None:
            return {}
        candidate = e.get("state") or {}
        if candidate.get("__PRIOR_STEP") == action_name:
            return candidate
        epoch, seq = key
        for later_key in sorted(k for k in ends if k[0] == epoch and k[1] > seq):
            later_state = ends[later_key].get("state") or {}
            if later_state.get("__PRIOR_STEP") == action_name:
                return later_state
        return candidate

    rows: list[StepRow] = []
    for key in sorted(begins):
        epoch, seq = key
        b = begins[key]
        e = ends.get(key)
        if e is None:
            rows.append(
                StepRow(
                    seq=seq,
                    action=b.get("action", "?"),
                    started=b.get("start_time", ""),
                    duration_ms=None,
                    status="running",
                    error_summary=None,
                    state_summary={},
                    epoch=epoch,
                )
            )
            continue
        rows.append(_completed_row(seq, b, e, _post_state(key, b.get("action", "?")), epoch))
    return rows


def _exception_summary(exc: str) -> str:
    """Pull the human-meaningful message out of a stored exception.

    Tracker exceptions are full tracebacks; the bare last line is often a
    stray `)` from a multi-line call. Prefer the last line that looks like
    `SomeError: message`, else the last non-empty line.
    """
    lines = [ln.rstrip() for ln in exc.strip().splitlines() if ln.strip()]
    if not lines:
        return "exception"
    for ln in reversed(lines):
        if re.match(r"^[A-Za-z_][\w.]*(Error|Exception|Failed|Warning):", ln.strip()):
            return ln.strip()[:160]
    return lines[-1].strip()[:160]


def _parse_ts_local(ts: str) -> datetime | None:
    """Parse an ISO timestamp into naive local time.

    Burr's tracker writes naive local timestamps; the refusal/ledger sidecars
    write UTC with offset. Rendering both through this keeps every CLI view
    in one zone (local) so events from the two sources line up.
    """
    try:
        when = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if when.tzinfo is not None:
        return when.astimezone().replace(tzinfo=None)
    return when


def _ts_sort_key(row: StepRow) -> tuple[datetime, int]:
    when = _parse_ts_local(row.started)
    return (when or datetime.min, row.seq)


def _duration_ms(start: str, end: str) -> float | None:
    s = _parse_ts_local(start)
    e = _parse_ts_local(end)
    if s is None or e is None:
        return None
    return round((e - s).total_seconds() * 1000, 1)


def _short_value(value: Any, *, limit: int = 28) -> str:
    if isinstance(value, str):
        s = value
    elif isinstance(value, (list, tuple)):
        s = f"[{len(value)} items]"
    elif isinstance(value, dict):
        s = f"{{{len(value)} keys}}"
    else:
        s = str(value)
    if len(s) > limit:
        return s[: limit - 1] + "…"
    return s


def _state_diff_text(
    state: dict[str, Any],
    prev_state: dict[str, Any] | None,
    *,
    max_items: int = 4,
) -> str:
    """Show only what changed since the previous step.

    For step 0 (no prev), show non-empty fields. For subsequent steps,
    show keys whose value differs from prev. This is what makes the
    timeline scan-able: each row says "this step changed X, Y".
    """
    if prev_state is None:
        changed = {k: v for k, v in state.items() if v not in (None, "", [], {}, False)}
    else:
        changed = {k: v for k, v in state.items() if prev_state.get(k) != v}
    if not changed:
        return "(no state change)"
    items = list(changed.items())[:max_items]
    parts = [f"{k}={_short_value(v)}" for k, v in items]
    if len(changed) > max_items:
        parts.append(f"+{len(changed) - max_items}")
    return ", ".join(parts)


def _display_local(ts: str) -> str:
    """Full local-time rendering of an ISO timestamp, for report bodies."""
    when = _parse_ts_local(ts)
    return when.isoformat(timespec="seconds") if when is not None else ts


def _short_ts(ts: str) -> str:
    when = _parse_ts_local(ts)
    if when is not None:
        return when.strftime("%H:%M:%S")
    if "T" in ts:
        return ts.split("T", 1)[1].split(".", 1)[0]
    return ts


def _relative_when(ts: str) -> str:
    """Render an ISO timestamp as a short relative-time label (3m, 2h, 4d)."""
    if not ts:
        return ""
    when = _parse_ts_local(ts)
    if when is None:
        return ts
    delta = datetime.now() - when
    s = int(delta.total_seconds())
    if s < 0:
        return ts.split("T", 1)[-1].split(".", 1)[0]
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _status_text(status: str) -> Text:
    if status == "ok":
        return Text("⊢", style="ok")
    if status == "error":
        return Text("×", style="err")
    if status == "empty":
        return Text("∅", style="muted")
    return Text("•", style="running")


def _build_steps_table(
    rows: list[StepRow], *, project: str, app_id: str, title_suffix: str = ""
) -> Table:
    title = f"[header]{project}[/] / [muted]{app_id}[/]"
    if title_suffix:
        title += f"  {title_suffix}"
    table = Table(
        title=title,
        title_justify="left",
        expand=True,
        show_lines=False,
        border_style="muted",
    )
    table.add_column("seq", justify="right", width=4, no_wrap=True, style="muted")
    table.add_column("time", width=8, no_wrap=True, style="subtle")
    table.add_column("", width=1, no_wrap=True)  # status glyph
    table.add_column("action", style="action", no_wrap=True)
    table.add_column("ms", justify="right", width=7, no_wrap=True, style="muted")
    table.add_column("state / error")
    prev_state: dict[str, Any] | None = None
    last_epoch = 0
    for r in rows:
        if r.epoch > last_epoch:
            # Burr restarted sequence ids here: the session was reset. Mark
            # it so the repeated seq numbers below read as a second run, not
            # a rendering bug, and the pre-reset steps stay in the record.
            table.add_row("", "", Text("↺", style="muted"), Text("(reset_session)", "muted"))
            last_epoch = r.epoch
            prev_state = None
        if r.status == "error":
            state_cell = Text(r.error_summary or "error", style="err")
        elif r.status == "running":
            state_cell = Text("(running...)", style="running")
        else:
            state_cell = Text(_state_diff_text(r.state_summary, prev_state), style="subtle")
        ms = "" if r.duration_ms is None else f"{r.duration_ms:.0f}"
        table.add_row(
            str(r.seq),
            _short_ts(r.started),
            _status_text(r.status),
            r.action,
            ms,
            state_cell,
        )
        if r.status != "error":
            prev_state = r.state_summary
    return table


def _scan_app_entry(app_dir: Path, *, show_all: bool) -> dict[str, Any] | None:
    """Build one ``sessions ls`` row from an app-dir, or ``None`` to skip."""
    log = app_dir / "log.jsonl"
    size = log.stat().st_size if log.exists() else 0
    rows = _read_steps(log) if size > 0 else []
    if not rows and not show_all:
        # FastMCP creates a tracker entry per Client connect; hide unadvanced.
        return None
    return {
        "app_id": app_dir.name,
        "mtime": datetime.fromtimestamp(app_dir.stat().st_mtime).isoformat(timespec="seconds"),
        "size_bytes": size,
        "steps": len(rows),
        "last_action": rows[-1].action if rows else "(empty)",
        "last_status": rows[-1].status if rows else "empty",
    }
