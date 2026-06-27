"""``LazyTrackingClient``: persist on first step, not on app-create.

Burr's ``LocalTrackingClient.post_application_create`` writes ``graph.json`` +
``metadata.json`` and opens the log handle the instant an Application is built
with a tracker. A session that only answers reads (an MCP discovery/probe
connection that reads ``theodosia://session`` or ``://state`` during discovery)
then leaves an empty dir. This subclass defers that write to the first persist
hook, so read-only sessions touch no disk: "app built" no longer means "dir on
disk"; "a step ran" does.

It also guards ``_log_child_relationships`` so a cross-project
``spawning_parent`` never fabricates a metadata-less parent dir (Burr
``makedirs`` the parent unconditionally, which breaks the Burr UI's
"load every dir as an app" assumption).

Reaches into Burr internals (the deferred ``post_application_create`` payload,
the persist-hook set, ``_log_child_relationships``); the project pins Burr to a
minor range for exactly this.
"""

from __future__ import annotations

import os
from typing import Any

from burr.tracking.client import LocalTrackingClient


class LazyTrackingClient(LocalTrackingClient):
    """A ``LocalTrackingClient`` that persists on first step, not on app-create.

    Construct it inside the factory (one per session), not hoisted and shared
    across sessions: the ``_flushed`` guard is per-instance, so a shared client
    would flush once and then route every later session's writes to the first
    session's open log handle. Theodosia's factory pattern already does this.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pending_create: dict[str, Any] | None = None
        self._flushed = False

    def copy(self) -> LazyTrackingClient:
        # Burr's base copy() returns a stock LocalTrackingClient (eager); keep
        # laziness on any copied tracker (e.g. a future sub-app spawn).
        return LazyTrackingClient(
            project=self.project_id,
            storage_dir=self.raw_storage_dir,
            serde_kwargs=self.serde_kwargs,
        )

    def post_application_create(self, **kwargs: Any) -> None:
        # Stash the create payload; flush it on the first persist hook instead
        # of writing graph.json/metadata.json/log handle here.
        self._pending_create = kwargs

    def _flush_create(self) -> None:
        if not self._flushed and self._pending_create is not None:
            super().post_application_create(**self._pending_create)
            self._flushed = True

    def ensure_persisted(self) -> None:
        """Force the deferred create to flush now (idempotent).

        Theodosia calls this before a write that is not a Burr persist hook,
        e.g. recording a refusal — an invalid transition never reaches
        ``pre_run_step``, but it is still a real interaction that should leave a
        proper dir (graph.json + metadata.json), not a metadata-less one.
        """
        self._flush_create()

    # Every Burr hook that writes to ``self.f`` flushes the deferred create
    # first. The ``_flushed`` guard makes this a no-op after the first step, so
    # covering all eight write hooks is cheap and leaves no bypass even if Burr
    # reorders them (``self.f`` only exists once the create is flushed).
    def pre_run_step(self, **kwargs: Any) -> Any:  # type: ignore[override]
        self._flush_create()
        return super().pre_run_step(**kwargs)

    def pre_start_stream(self, **kwargs: Any) -> Any:
        self._flush_create()
        return super().pre_start_stream(**kwargs)

    def post_run_step(self, **kwargs: Any) -> Any:  # type: ignore[override]
        self._flush_create()
        return super().post_run_step(**kwargs)

    def pre_start_span(self, **kwargs: Any) -> Any:
        self._flush_create()
        return super().pre_start_span(**kwargs)

    def post_end_span(self, **kwargs: Any) -> Any:
        self._flush_create()
        return super().post_end_span(**kwargs)

    def do_log_attributes(self, **kwargs: Any) -> Any:
        self._flush_create()
        return super().do_log_attributes(**kwargs)

    def post_stream_item(self, **kwargs: Any) -> Any:
        self._flush_create()
        return super().post_stream_item(**kwargs)

    def post_end_stream(self, **kwargs: Any) -> Any:
        self._flush_create()
        return super().post_end_stream(**kwargs)

    def _log_child_relationships(
        self,
        fork_parent_pointer_model: Any,
        spawn_parent_pointer_model: Any,
        app_id: str,
        partition_key: str | None = None,
    ) -> None:
        # Cross-project guard: only log to a parent whose dir already exists
        # under this storage_dir. Burr otherwise ``makedirs`` a metadata-less
        # parent dir, which the Burr UI then fails to load as an app. The
        # child's own ``spawning_parent_pointer`` in its ``metadata.json``
        # preserves the lineage regardless of the parent-side ``children.jsonl``.
        def _present(pointer: Any) -> bool:
            return pointer is not None and os.path.exists(
                os.path.join(self.storage_dir, pointer.app_id)
            )

        fork = fork_parent_pointer_model if _present(fork_parent_pointer_model) else None
        spawn = spawn_parent_pointer_model if _present(spawn_parent_pointer_model) else None
        if fork is None and spawn is None:
            return
        super()._log_child_relationships(fork, spawn, app_id, partition_key)
