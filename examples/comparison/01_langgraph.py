"""Rendering 1 of 3: LangGraph's own "Prompt chaining" workflow, verbatim.

Source (structure and gate are reproduced exactly; the three ``llm.invoke`` calls
are the shared deterministic stub so it runs key-free):
    https://docs.langchain.com/oss/python/langgraph/workflows-agents  (Prompt chaining)

This is the status-quo way to express a fixed multi-step chain in LangGraph: a
typed State, one node per step, and a conditional edge for the punchline gate.

Run:  ../../.venv/bin/python 01_langgraph.py
"""

from __future__ import annotations

import os
import sys
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

sys.path.insert(0, os.path.dirname(__file__))
from _shared import check_punchline, write


# graph:begin
class State(TypedDict, total=False):
    topic: str
    joke: str
    improved_joke: str
    final_joke: str


def generate_joke(state: State) -> dict:
    """First LLM call to generate initial joke"""
    return {"joke": write("generate", topic=state["topic"])}


def gate(state: State) -> str:
    """Gate function to check if the joke has a punchline"""
    return check_punchline(state["joke"])


def improve_joke(state: State) -> dict:
    """Second LLM call to improve the joke"""
    return {"improved_joke": write("improve", text=state["joke"])}


def polish_joke(state: State) -> dict:
    """Third LLM call for final polish"""
    return {"final_joke": write("polish", text=state["improved_joke"])}


def build_graph():
    workflow = StateGraph(State)
    workflow.add_node("generate_joke", generate_joke)
    workflow.add_node("improve_joke", improve_joke)
    workflow.add_node("polish_joke", polish_joke)
    workflow.add_edge(START, "generate_joke")
    workflow.add_conditional_edges("generate_joke", gate, {"Fail": "improve_joke", "Pass": END})
    workflow.add_edge("improve_joke", "polish_joke")
    workflow.add_edge("polish_joke", END)
    return workflow.compile()


# graph:end


def run_langgraph(topic: str) -> dict[str, str]:
    out = build_graph().invoke({"topic": topic})
    return {"final": out.get("final_joke") or out["joke"], "verdict": check_punchline(out["joke"])}


if __name__ == "__main__":
    topic = sys.argv[1] if len(sys.argv) > 1 else "cat"
    print(f"[langgraph] topic={topic}")
    for k, v in run_langgraph(topic).items():
        print(f"  {k}: {v}")
