"""Shared pieces for the three-way comparison, on LangGraph's own workflow.

The workflow is LangGraph's canonical "Prompt chaining" example, verbatim from
their docs: generate a joke, gate on whether it has a punchline, and if not,
improve then polish it.

    https://docs.langchain.com/oss/python/langgraph/workflows-agents  (Prompt chaining)

    START -> generate_joke -> check_punchline --Pass--> END
                                    |
                                   Fail
                                    v
                              improve_joke -> polish_joke -> END

`01_langgraph.py` is that graph verbatim. `02` and `03` render the same graph in
Burr and over Theodosia, so the only variable is architecture. The three LLM
calls are replaced by a deterministic stub (``write``) so every rendering runs
key-free and produces the identical joke; the live model path stays opt-in.
"""

from __future__ import annotations

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition

from theodosia import ValidationFailed


def write(step: str, topic: str = "", text: str = "") -> str:
    """Deterministic stand-in for the three LLM calls in the chain.

    Mirrors the docs' prompts: generate about a topic, add wordplay, add a twist.
    Kept punchline-free on generate so the gate routes through improve+polish.
    """
    if step == "generate":
        return f"A {topic} walked into a bar"
    if step == "improve":
        return f"{text} and ordered a byte to eat"
    if step == "polish":
        return f"{text}. Turns out it was a robot all along!"
    raise ValueError(f"unknown step {step!r}")


def check_punchline(joke: str) -> str:
    """Gate function to check if the joke has a punchline"""
    # Simple check - does the joke contain "?" or "!"
    return "Pass" if ("?" in joke or "!" in joke) else "Fail"


# ── the Burr graph (shared by 02_orchestrator and 03_theodosia) ───────
# Each Burr action maps 1:1 to a LangGraph node in 01_langgraph.py; the docstrings
# are the tutorial's verbatim. The only difference is the gate: LangGraph reads
# check_punchline in a conditional edge; Burr stores its result as `verdict` and
# reads that in the transition condition.
# graph:begin
@action(reads=[], writes=["topic", "joke", "verdict"])
def generate_joke(state: State, topic: str) -> State:
    """First LLM call to generate initial joke"""
    joke = write("generate", topic=topic)
    return state.update(topic=topic, joke=joke, verdict=check_punchline(joke))


@action(reads=["joke"], writes=["improved_joke"])
def improve_joke(state: State) -> State:
    """Second LLM call to improve the joke"""
    return state.update(improved_joke=write("improve", text=state["joke"]))


@action(reads=["improved_joke"], writes=["final_joke"])
def polish_joke(state: State) -> State:
    """Third LLM call for final polish"""
    return state.update(final_joke=write("polish", text=state["improved_joke"]))


def build_joke_app() -> ApplicationBuilder:
    return (
        ApplicationBuilder()
        .with_actions(
            generate_joke=generate_joke, improve_joke=improve_joke, polish_joke=polish_joke
        )
        # LangGraph: add_conditional_edges("generate_joke", check_punchline,
        #            {"Fail": "improve_joke", "Pass": END})
        .with_transitions(
            ("generate_joke", "improve_joke", Condition.expr("verdict == 'Fail'")),
            # LangGraph: add_edge("improve_joke", "polish_joke")
            ("improve_joke", "polish_joke"),
        )
        .with_state(topic="", joke="", verdict="")
        .with_entrypoint("generate_joke")
    )


# graph:end


# ── Theodosia-only addition: a server-validated slot on generate_joke ──
# gate:begin
def topic_gate(state: dict, inputs: dict) -> dict | None:
    if not str(inputs.get("topic") or "").strip():
        raise ValidationFailed("a non-empty topic is required", details={"param": "topic"})
    return None


generate_joke._theodosia_validator = topic_gate  # type: ignore[attr-defined]
# gate:end


def final_text(state: dict) -> str:
    """The chain's output: the polished joke if it ran, else the original (Pass path)."""
    return state.get("final_joke") or state["joke"]


EXPECTED = {
    "cat": "A cat walked into a bar and ordered a byte to eat. Turns out it was a robot all along!"
}
