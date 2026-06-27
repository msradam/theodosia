"""theodosia: mount Burr Applications as MCP servers.

State lives on the server. Every mounted server exposes a constant
four-tool surface (``step``, ``reset_session``, ``fork_at``,
``fork_from_past``) regardless of FSM complexity; the action namespace
lives in ``step``'s argument schema and at ``theodosia://graph``. Plus two
synthetic tools from FastMCP's ``ResourcesAsTools`` transform
(``list_resources``, ``read_resource``) for clients that don't
implement native ``resources/read`` (IBM Bob Shell as of mid-2026).

``TOOLS`` and ``DYNAMIC`` serving modes were carved into
``theodosia._experimental.modes`` once ``STEP`` became the sole product;
the ``ServingMode`` enum keeps ``STEP`` as its only member so callers
passing ``mode=ServingMode.STEP`` keep working.
"""

from typing import Any

from theodosia._exceptions import ValidationFailed
from theodosia._lazy_tracker import LazyTrackingClient
from theodosia.adapter import (
    ServingMode,
    current_mcp_context,
    mount,
    mount_multi,
    spawn_subapp,
)
from theodosia.assembly import Assembly
from theodosia.cli import build_cli
from theodosia.cli import run as run_cli
from theodosia.drive import drive_claude
from theodosia.importing import ToolSpec, burr_app_from_fastmcp
from theodosia.ledger import HashChainedLedger, verify_ledger
from theodosia.upstream import (
    ERROR,
    MALFORMED,
    OK,
    SourceResult,
    UpstreamError,
    UpstreamManager,
    bind_upstream,
    call_upstream,
    classify_payload,
    confidence_label,
    coverage,
    safe_upstream,
)


def _resolve_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("theodosia")
    except PackageNotFoundError:
        return "0+unknown"


__version__ = _resolve_version()


def tracker(
    project: str, storage_dir: str | None = None, *, lazy: bool = True, **kwargs: Any
) -> Any:
    """A Burr tracking client that defaults its store to ``~/.{prog_name}``.

    Use this in your builder (``.with_tracker(theodosia.tracker("my-project"))``)
    to keep LLM-driven session traces separate from code-driven Burr runs, which
    use Burr's own ``~/.burr`` default. It is a thin wrapper; pass any
    ``LocalTrackingClient`` keyword through.

    ``lazy`` (default) returns a :class:`LazyTrackingClient`, which persists on
    the first step rather than on app-create, so a session that only answers
    reads (an MCP discovery/probe connection) leaves no empty dir on disk. Pass
    ``lazy=False`` for the stock ``LocalTrackingClient`` (writes the dir the
    instant the app is built).

    Resolution order for ``storage_dir``: explicit argument wins; otherwise
    the brand-specific env var (e.g. ``HELIOS_HOME`` for a ``helios`` CLI);
    otherwise ``THEODOSIA_HOME``; otherwise the ``home=`` set via
    :func:`build_cli`; otherwise ``~/.{prog_name}`` derived from the CLI's
    brand name (``~/.theodosia`` for the default CLI, ``~/.helios`` for a
    ``helios`` rebrand, etc.). This keeps a downstream rebrand's tracker
    writes and CLI reads pointed at the same root without any extra config.

    Note for standalone scripts (seeding/tests that build the app *outside*
    the CLI process, where ``build_cli`` never ran): the branded ``home`` is
    not visible, so writes land in ``~/.theodosia``. Pass ``storage_dir``
    explicitly, or set the brand-specific env var, to land them in the
    branded store.
    """
    import contextlib
    import os

    if storage_dir is None:
        with contextlib.suppress(Exception):
            from theodosia.cli._branding import _BRANDING, _prog_slug

            slug = _prog_slug(_BRANDING.prog_name)
            env_key = f"{slug.upper().replace('-', '_')}_HOME"
            env_val = os.environ.get(env_key) or os.environ.get("THEODOSIA_HOME")
            storage_dir = env_val or (
                str(_BRANDING.home) if _BRANDING.home is not None else f"~/.{slug}"
            )
    if storage_dir is None:
        storage_dir = "~/.theodosia"
    if lazy:
        return LazyTrackingClient(project=project, storage_dir=storage_dir, **kwargs)
    from burr.tracking.client import LocalTrackingClient

    return LocalTrackingClient(project=project, storage_dir=storage_dir, **kwargs)


__all__ = [
    "ERROR",
    "MALFORMED",
    "OK",
    "Assembly",
    "HashChainedLedger",
    "LazyTrackingClient",
    "ServingMode",
    "SourceResult",
    "ToolSpec",
    "UpstreamError",
    "UpstreamManager",
    "ValidationFailed",
    "__version__",
    "bind_upstream",
    "build_cli",
    "burr_app_from_fastmcp",
    "call_upstream",
    "classify_payload",
    "confidence_label",
    "coverage",
    "current_mcp_context",
    "drive_claude",
    "mount",
    "mount_multi",
    "run_cli",
    "safe_upstream",
    "spawn_subapp",
    "tracker",
    "verify_ledger",
]
