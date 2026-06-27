"""On-disk tracker helpers: locate the log file, read the trace, name the project.

Burr's ``LocalTrackingClient`` writes a per-session ``log.jsonl`` under
its ``storage_dir/<project>/<app_id>/``. These helpers wrap the
introspection a little so the rest of the adapter does not have to
reach into ``app._tracker`` directly.

Reaches into Burr's ``app._tracker`` slot. We pin Burr to a minor
version range in ``pyproject.toml`` for exactly this reason.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from burr.core import Application

# Cap ``theodosia://trace`` response to the last N records. Burr's
# tracker is append-only; long-running sessions accumulate; an MCP
# client doesn't want the full multi-MB log returned over the wire.
_TRACE_MAX_ENTRIES = 1000


def _tracker_project(app: Application[Any]) -> str | None:
    """Return the LocalTrackingClient project name, or None.

    Surfaced on every step/fork meta-tool response so even collapsed
    tool-result views in MCP clients carry enough to locate the
    session's data on disk (``~/.burr/<project>/<app_id>/``).
    """
    try:
        from burr.tracking.client import LocalTrackingClient
    except ImportError:
        return None
    tracker = getattr(app, "_tracker", None)
    if not isinstance(tracker, LocalTrackingClient):
        return None
    return tracker.project_id


def _tracker_file(app: Application[Any], filename: str) -> Path | None:
    """Locate a file in this Application's Burr tracker dir, safely.

    Reads ``app._tracker`` which is Burr's internal slot for the
    ``LocalTrackingClient``. We pin Burr to a minor version range
    because of this and similar internals (see ``pyproject.toml``).
    Returns ``None`` when the Application has no tracker, or has a
    non-local one, or the resolved path escapes the tracker's own
    storage directory.
    """
    try:
        from burr.tracking.client import LocalTrackingClient
    except ImportError:
        return None
    tracker = getattr(app, "_tracker", None)
    if not isinstance(tracker, LocalTrackingClient):
        return None
    try:
        storage_dir = Path(tracker.storage_dir).expanduser().resolve()
        path = (storage_dir / app.uid / filename).resolve()
    except (OSError, AttributeError):
        return None
    # Defence in depth: the computed path must sit under the tracker's
    # storage dir. If app.uid contained a traversal sequence (it shouldn't,
    # Burr generates UUIDs, but belt-and-braces), refuse to read it.
    try:
        path.relative_to(storage_dir)
    except ValueError:
        return None
    return path


def _tracker_log_path(app: Application[Any]) -> Path | None:
    """Locate the on-disk action-step log for this Application's tracker."""
    try:
        from burr.tracking.client import LocalTrackingClient
    except ImportError:
        return None
    return _tracker_file(app, LocalTrackingClient.LOG_FILENAME)


def _children_path(app: Application[Any]) -> Path | None:
    """Locate the ``children.jsonl`` Burr writes for spawned/forked sub-apps.

    Burr appends one record per ``with_spawning_parent`` / fork link into
    the *parent* app's dir, so this surfaces native sub-applications a user
    spawns inside an action, independent of Theodosia's own ``spawn_subapp``.
    """
    try:
        from burr.tracking.client import LocalTrackingClient
    except ImportError:
        return None
    return _tracker_file(app, LocalTrackingClient.CHILDREN_FILENAME)


def _read_trace(path: Path, *, tail: int = _TRACE_MAX_ENTRIES) -> list[dict[str, Any]]:
    """Read a JSONL trace file and return the last ``tail`` records.

    Malformed lines are skipped silently rather than tanking the whole
    response. The cap is in place because Burr's tracker is append-only;
    long-running sessions accumulate; an MCP client doesn't want the
    full 50 MB log returned over the wire on every read.
    """
    entries: list[dict[str, Any]] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if tail and len(entries) > tail:
        entries = entries[-tail:]
    return entries
