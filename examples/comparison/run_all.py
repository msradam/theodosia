"""Run LangGraph's prompt-chaining workflow through all three renderings.

Prints each rendering's output (they must match, since the only variable is the
architecture), then the lines-of-code the graph costs in each framework. With
COMPARISON_LIVE=1, also hands rendering 03 to a real Claude agent.

Run:  ../../.venv/bin/python run_all.py
      COMPARISON_LIVE=1 ../../.venv/bin/python run_all.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _shared import EXPECTED  # noqa: E402


def _load(filename: str):
    spec = importlib.util.spec_from_file_location(filename[:-3], HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # so get_type_hints can resolve annotations
    spec.loader.exec_module(mod)
    return mod


def code_lines(path: Path, begin: str = "# graph:begin", end: str = "# graph:end") -> int:
    """Count code lines between the markers, ignoring blanks, comments, docstrings."""
    inside = in_doc = False
    n = 0
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line == begin:
            inside = True
            continue
        if line == end:
            break
        if not inside:
            continue
        if in_doc:
            if '"""' in line or "'''" in line:
                in_doc = False
            continue
        if not line or line.startswith("#"):
            continue
        if line.startswith(('"""', "'''")):
            if line.count('"""') < 2 and line.count("'''") < 2:
                in_doc = True
            continue
        n += 1
    return n


def main() -> None:
    lg = _load("01_langgraph.py")
    burr = _load("02_burr_orchestrator.py")
    theo = _load("03_theodosia_mcp.py")

    topic = "cat"
    results = {
        "01 langgraph": lg.run_langgraph(topic),
        "02 burr orchestrator": burr.run_burr(topic),
        "03 theodosia mcp": theo.run_theodosia(topic),
    }

    print(f"same workflow (LangGraph prompt chaining), topic={topic!r}\n")
    finals = [r["final"] for r in results.values()]
    for name, r in results.items():
        print(f"  {name:<22} verdict={r['verdict']}")
    assert all(f == finals[0] for f in finals), "renderings disagreed"
    assert finals[0] == EXPECTED[topic]
    print(f"\n  all three agree: {finals[0]}\n")

    burr_loc = code_lines(HERE / "_shared.py")
    lg_loc = code_lines(HERE / "01_langgraph.py")
    gate_loc = code_lines(HERE / "_shared.py", "# gate:begin", "# gate:end")
    print("graph ceremony (code lines for the same chain; comments/docstrings excluded):")
    print(f"  01 langgraph      {lg_loc}")
    print(f"  02 burr           {burr_loc}")
    print(f"  03 theodosia      {burr_loc} + 1   (identical Burr graph + one mount() call)")
    print(f"    + {gate_loc} lines for the server-validated slot gate, enforced only under mount()")

    if os.environ.get("COMPARISON_LIVE") == "1":
        print("\n[live] handing rendering 03 to a real Claude agent (authed session)...")
        live = theo.drive_with_claude_agent(topic)
        if live is not None:
            for sf in live["slot_fills"]:
                print(f"    step {sf['action']:<14} inputs={sf['inputs']}")
            print(f"  cost_usd: {live['cost_usd']}")


if __name__ == "__main__":
    main()
