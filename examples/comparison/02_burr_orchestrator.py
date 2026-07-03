"""Rendering 2 of 3: the same prompt chain in Burr, orchestrator style.

The script owns the loop and steps the graph directly. The LLM is a callee,
invoked inside each action. Same three steps and the same punchline gate as
``01_langgraph.py``; compare the wiring in ``_shared.build_joke_app`` with the
StateGraph in ``01``.

Run:  ../../.venv/bin/python 02_burr_orchestrator.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _shared import build_joke_app, final_text


def run_burr(topic: str) -> dict[str, str]:
    """Drive the joke chain as a plain Burr orchestrator, step by step."""
    app = build_joke_app().build()

    app.step(inputs={"topic": topic})  # generate_joke (entrypoint)
    while app.step() is not None:  # improve_joke, polish_joke, until terminal
        pass

    state = dict(app.state.get_all())
    return {"final": final_text(state), "verdict": state["verdict"]}


if __name__ == "__main__":
    topic = sys.argv[1] if len(sys.argv) > 1 else "cat"
    print(f"[burr orchestrator] topic={topic}")
    for k, v in run_burr(topic).items():
        print(f"  {k}: {v}")
