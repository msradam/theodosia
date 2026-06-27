"""Dependency-free state-machine diagram rendering.

The topology model and the Mermaid/DOT emitters read only a Burr
``Application``'s graph, so they are shared by the CLI ``render`` command
(which adds a rich terminal view on top) and the ``theodosia://graph/mermaid``
/ ``theodosia://graph/dot`` resources. Nothing here imports typer or rich.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from burr.core import Application


@dataclass
class _Topology:
    name: str
    entry: str | None
    actions: list[str]
    edges: list[tuple[str, str, str | None]]  # (from, to, condition)

    def out_edges(self, node: str) -> list[tuple[str, str | None]]:
        return [(to, cond) for frm, to, cond in self.edges if frm == node]

    def is_terminal(self, node: str) -> bool:
        return not any(frm == node for frm, _to, _c in self.edges)

    def has_self_loop(self, node: str) -> bool:
        return any(frm == node == to for frm, to, _c in self.edges)


def _condition_label(condition: Any) -> str | None:
    """Human label for a transition condition, or None for the default (always)."""
    name = getattr(condition, "name", None)
    return None if not name or name == "default" else name


def _topology_from_app(app: Application[Any], name: str) -> _Topology:
    """Read a Burr Application's graph into a renderable topology."""
    graph = app.graph
    entry = graph.entrypoint.name if getattr(graph, "entrypoint", None) else None
    actions = [a.name for a in graph.actions]
    edges = [(t.from_.name, t.to.name, _condition_label(t.condition)) for t in graph.transitions]
    return _Topology(name=name, entry=entry, actions=actions, edges=edges)


def _render_mermaid(topo: _Topology, *, conditions: bool) -> str:
    lines = ["stateDiagram-v2"]
    if topo.entry:
        lines.append(f"    [*] --> {topo.entry}")
    for frm, to, cond in topo.edges:
        label = f" : {cond}" if conditions and cond else ""
        lines.append(f"    {frm} --> {to}{label}")
    lines.extend(f"    {node} --> [*]" for node in topo.actions if topo.is_terminal(node))
    return "\n".join(lines)


def _render_dot(topo: _Topology, *, conditions: bool) -> str:
    lines = ["digraph G {", "    rankdir=LR;", "    node [shape=box, style=rounded];"]
    if topo.entry:
        lines.extend(("    __start__ [shape=point];", f'    __start__ -> "{topo.entry}";'))
    for frm, to, cond in topo.edges:
        label = f' [label="{cond}"]' if conditions and cond else ""
        lines.append(f'    "{frm}" -> "{to}"{label};')
    terminals = [node for node in topo.actions if topo.is_terminal(node)]
    if terminals:
        lines.append("    __end__ [shape=point];")
        lines.extend(f'    "{node}" -> __end__;' for node in terminals)
    lines.append("}")
    return "\n".join(lines)
