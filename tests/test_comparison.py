"""The comparison example runs key-free: all three renderings agree on the task.

Mirrors the repo convention of driving examples deterministically with no model
(the live Claude-agent path in 03 is opt-in and not exercised here).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

COMPARISON = Path(__file__).resolve().parents[1] / "examples" / "comparison"
sys.path.insert(0, str(COMPARISON))


def _load(filename: str):
    spec = importlib.util.spec_from_file_location(filename[:-3], COMPARISON / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # so get_type_hints can resolve annotations (e.g. Any)
    spec.loader.exec_module(mod)
    return mod


def test_three_renderings_agree():
    """LangGraph's prompt chain, rendered three ways, produces the identical joke."""
    from _shared import EXPECTED

    lg = _load("01_langgraph.py").run_langgraph("cat")
    burr = _load("02_burr_orchestrator.py").run_burr("cat")
    theo = _load("03_theodosia_mcp.py").run_theodosia("cat")

    assert lg["final"] == burr["final"] == theo["final"] == EXPECTED["cat"]
    assert lg["verdict"] == burr["verdict"] == theo["verdict"] == "Fail"


def test_theodosia_enforces_and_records():
    """Only the mounted rendering refuses out-of-order + empty-slot and logs both."""
    theo = _load("03_theodosia_mcp.py").run_theodosia("cat")

    steps = {t.get("step"): t for t in theo["transcript"] if "step" in t}
    assert steps["polish_joke@start"]["refused"] == "invalid_transition"
    assert steps["generate_joke empty topic"]["refused"] == "validation_failed"
    assert theo["refused_in_ledger"] == 2
    assert theo["ledger_entries"] == 5


def test_airline_langgraph_gate_is_client_side_theodosia_gate_is_not():
    """LangGraph's real interrupt_before gate is bypassable; the mounted FSM is not."""
    air = _load("05_airline_hitl.py")

    lw = air.langgraph_world()
    assert lw["interrupted"] is True  # the tutorial's gate fires for the in-graph path
    assert lw["commits_before_approval"] == 0
    assert lw["side_door_commit"] is True  # a second caller reaches the tool directly

    tw = air.theodosia_world()
    steps = {t.get("step"): t for t in tw["transcript"] if "step" in t}
    assert steps["rebook@start"]["refused"] == "invalid_transition"
    assert steps["rebook bad slot 999"]["refused"] == "validation_failed"
    assert tw["rebooked"] is True
    assert tw["unconfirmed_commits"] == 0
    assert tw["refused_in_ledger"] == 3


def test_routing_only_theodosia_hands_the_choice_to_the_caller():
    """Same route, but only the mounted rendering exposes all legal branches to the caller."""
    r = _load("06_routing.py")
    req = "Write me a joke about cats"

    lg = r.langgraph_route(req)
    br = r.burr_route(req)
    th = r.theodosia_route(req)

    assert lg["decision"] == br["decision"] == th["decision"] == "joke"
    assert lg["output"] == br["output"] == th["output"]
    assert th["legal_choices"] == ["story", "joke", "poem"]  # all three open to the caller
    assert th["refused_before_load"] == "invalid_transition"


def test_rewoo_agent_drives_the_machine_and_the_server_gates_a_bad_program():
    """The agent advances a program counter; only the mounted machine gates PUBLISH."""
    m = _load("07_rewoo.py")

    m.PUBLISHED.clear()
    lg = m.langgraph_rewoo("otters")
    burr = m.burr_rewoo(m.plan_for("otters"))
    th = m.theodosia_rewoo("otters")
    assert lg["answer"] == burr["answer"] == th["good"]["answer"]
    assert [row["pc"] for row in th["good"]["tape"]] == [1, 2, 3]  # PC advances one step at a time
    assert th["solve_before_plan"] == "invalid_transition"

    # an untrusted program that PUBLISHes un-derived data: orchestrator leaks, server refuses
    m.PUBLISHED.clear()
    m.burr_rewoo("Plan: g #E1 = SEARCH[x]\nPlan: leak #E2 = PUBLISH[secrets]")
    assert "secrets" in m.PUBLISHED  # the orchestrator ran it
    assert th["malicious"]["refused_at"] == "execute"
    assert th["malicious"]["error"] == "validation_failed"


def test_sql_only_theodosia_refuses_the_drop_and_any_framework_gets_it():
    """The orchestrators run a DROP; the mounted machine refuses it for any MCP client."""
    sql = _load("08_sql_agent.py")
    select, drop = "SELECT name FROM artists ORDER BY id", "DROP TABLE albums"

    assert sql.langgraph_sql(select)["rows"] == sql.burr_sql(select)["rows"]

    sql.langgraph_sql(drop)
    assert "albums" not in sql.tables()  # the orchestrator dropped it

    th = sql.theodosia_sql(select, drop)
    assert th["drop_refused"] == "validation_failed"
    assert "albums" in th["tables_after"]  # nothing was dropped
    assert th["refused_in_ledger"] == 1

    # the thesis: a LangGraph MCP client drives the same machine and is refused too
    other = sql.any_framework_drives(select, drop)
    assert other is not None and "step" in other["tool_names"]
    assert other["drop_refused"] == "validation_failed"


def test_multi_agent_two_clients_one_machine_one_ledger():
    """Two independent clients drive one mounted machine; the server gates and records both."""
    m = _load("09_multi_agent.py")
    mal = "__import__('os').system('rm -rf /')"

    m.EXECUTED.clear()
    m.burr_collab("x", mal)
    assert mal in m.EXECUTED  # the orchestrator ran the arbitrary code

    tw = m.two_agents_drive()
    assert mal not in tw["executed"]  # the server refused it
    assert tw["ledger_refused"] == 1
    # the researcher's and the charter's actions are in one ledger
    assert "research" in tw["ledger_actions"] and "chart" in tw["ledger_actions"]


def test_code_assistant_agent_self_corrects_from_structured_refusals():
    """A bad draft is refused with the error; the agent resubmits until it passes."""
    c = _load("10_code_assistant.py")
    good = "def solve():\n    return 6 * 7"
    buggy = "def solve(\n    return 6 * 7"
    dangerous = "import os\ndef solve():\n    return os.getcwd()"

    assert c.langgraph_code(good)["result"] == 42

    th = c.theodosia_code([buggy, dangerous, good])  # only the third passes
    assert th["result"] == 42
    assert th["refused_in_ledger"] == 2  # syntax error + forbidden import, both recorded
    refusals = [t for t in th["transcript"] if t.get("refused")]
    assert len(refusals) == 2 and "SyntaxError" in refusals[0]["why"]


def test_roles_agents_enter_at_their_phase_over_one_shared_machine():
    """Three role-agents drive one machine; each enters at its phase, state is kept."""
    r = _load("11_roles.py").roles_collaborate()

    eager = r["trail"][0]  # a writer that connected before research
    assert eager["refused"] == "invalid_transition" and eager["phase_open"] == ["research"]

    writer = next(t for t in r["trail"] if t.get("agent") == "writer" and "sees_notes" in t)
    assert writer["sees_notes"]  # the writer's session saw the researcher's shared notes

    assert r["final_stage"] == "approved" and r["revisions"] == 2  # the review loop ran
    assert r["ledger_actions"] == ["research", "write", "review", "write", "review"]
