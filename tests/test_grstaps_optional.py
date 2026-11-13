"""GRSTAPS-X is optional: with none of its pieces installed (heracles_agents'
prompts.ENGINES is ("fast_downward",), no omniplanner.grstaps_planner, no
TaskScheduleMsg, no GRSTAPS-X planner in the ROS graph) the console is Fast
Downward only -- no errors, nothing offering or asking for GRSTAPS-X."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace as NS

import pytest

from adt4_console import ros_bridge as rb_mod
from adt4_console.chat import (
    CONSOLE_GOAL_NOTES,
    CONSOLE_GOAL_NOTES_FD_ONLY,
    GOAL_PROMPT_NAMES,
    ChatService,
    prompts_have_grstaps,
)
from adt4_console.mission import grstaps_engine, llm_planner, omni_planner
from adt4_console.ros_bridge import NO_GRSTAPS, RosBridge
from tests.conftest import needs_rclpy
from tests.test_chat_roster import chat

FD_ONLY = NS(ENGINES=("fast_downward",))


def _running_grstaps(c):
    """The base runs GRSTAPS-X (goal_manager forwards to it)."""
    c.ros.available = True
    c.ros.goal_manager_engine = lambda: "grstaps"
    c.ros.find_plugin = lambda kind: {"name": "grstaps_planner"}
    return c


# ---------------------------------------------------------------- engine lists
def test_prompts_without_grstaps_have_no_grstaps_goals():
    assert not prompts_have_grstaps(None)
    assert not prompts_have_grstaps(FD_ONLY)
    assert not prompts_have_grstaps(NS())  # no ENGINES at all
    assert prompts_have_grstaps(NS(ENGINES=("fast_downward", "grstaps")))


def test_chat_toggle_is_fast_downward_only_without_grstaps_rules():
    """A GRSTAPS-X planner running is not enough: heracles_agents has no rules for it."""
    c = _running_grstaps(chat())
    c.goal_prompts = FD_ONLY
    assert [e["id"] for e in c.chat_engines()] == ["fast_downward"]
    assert [e["id"] for e in c.usage()["engines"]] == ["fast_downward"]
    assert c.chat_engines()[0]["available"] is True


def test_chat_toggle_is_fast_downward_only_without_a_grstaps_planner():
    c = chat()
    c.goal_prompts = NS(ENGINES=("fast_downward", "grstaps"))
    assert [e["id"] for e in c.chat_engines()] == ["fast_downward"]  # no ROS
    c.ros.available = True
    c.ros.goal_manager_engine = lambda: "fast_downward"
    c.ros.find_plugin = lambda kind: None
    c.ros.grstaps_live = lambda: None
    [fd] = c.chat_engines()
    assert fd["id"] == "fast_downward" and fd["repair"] is True and fd["reason"] is None
    # nothing tells the operator to start a GRSTAPS-X session
    assert "session" not in str(c.chat_engines())


def test_chat_toggle_offers_grstaps_when_installed_and_running():
    c = _running_grstaps(chat())
    c.goal_prompts = NS(ENGINES=("fast_downward", "grstaps"))
    eng = {e["id"]: e for e in c.chat_engines()}
    assert eng["grstaps"]["available"] and eng["grstaps"]["repair"] is True
    assert eng["fast_downward"]["repair"] is False


def test_mission_engines_list_fast_downward_only_without_grstaps(monkeypatch):
    # omniplanner without grstaps_planner: the import fails
    monkeypatch.setitem(sys.modules, "omniplanner.grstaps_planner", None)
    assert not grstaps_engine.installed()
    assert "GRSTAPS-X is not installed" in omni_planner.available("grstaps")
    assert [e["id"] for e in omni_planner.engines()] == ["fast_downward"]


def test_mission_engines_grey_out_an_installed_grstaps_without_a_license(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(grstaps_engine, "_modules", lambda: ({}, None))
    monkeypatch.setattr(grstaps_engine, "itags_binary", lambda: "/usr/bin/true")
    monkeypatch.setenv("ADT4_GUROBI_LICENSE", str(tmp_path / "missing.lic"))
    gr = {e["id"]: e for e in omni_planner.engines()}["grstaps"]
    assert not gr["available"] and "no Gurobi license" in gr["reason"]


def test_engines_route_without_grstaps(settings, monkeypatch):
    from tests.conftest import make_client

    monkeypatch.setitem(sys.modules, "omniplanner.grstaps_planner", None)
    client, _ = make_client(settings)
    with client:
        body = client.get("/api/missions/plan/engines").json()
    assert body["default"] == "fast_downward"
    assert [e["id"] for e in body["engines"]] == ["fast_downward"]


# ------------------------------------------------------------- chat goal text
def test_fd_only_goal_tool_has_no_grstaps():
    tool = ChatService._T_PROPOSE_GOAL_FD
    props = tool["input_schema"]["properties"]
    assert props["engine"]["enum"] == ["fast_downward"]
    assert (
        "before" not in props["constraints"]["items"]["properties"]["predicate"]["enum"]
    )
    assert "grstaps" not in tool["description"].lower()
    # the rest of the description is Fast Downward's, unchanged
    full = ChatService._T_PROPOSE_GOAL["description"]
    assert tool["description"].startswith(full[: full.index(" With engine='grstaps'")])
    assert tool["description"].endswith(full[full.index(" The goal is checked") :])
    # the full tool is untouched
    assert (
        "grstaps"
        in ChatService._T_PROPOSE_GOAL["input_schema"]["properties"]["engine"]["enum"]
    )


def test_fd_only_notes_differ_only_in_the_engine_bullet():
    assert (
        "GRSTAPS" not in CONSOLE_GOAL_NOTES_FD_ONLY
        and "grstaps" not in CONSOLE_GOAL_NOTES_FD_ONLY
    )
    full, fd = CONSOLE_GOAL_NOTES.splitlines(), CONSOLE_GOAL_NOTES_FD_ONLY.splitlines()
    assert len(full) == len(fd)
    changed = [(a, b) for a, b in zip(full, fd) if a != b]
    assert len(changed) == 1 and changed[0][1].startswith(
        "- engine: Fast Downward plans"
    )


def test_tools_without_grstaps_offer_the_fd_only_goal_tool():
    c = chat()
    c.goal_prompts = FD_ONLY
    tools = c._tools(None, turn=c.turn)
    assert (
        ChatService._T_PROPOSE_GOAL_FD in tools
        and ChatService._T_PROPOSE_GOAL not in tools
    )
    # a mission chat whose GRSTAPS-X cannot plan: plan_ground_mission is Fast Downward only
    c.turn.mission = {"omniplanner": True, "grstaps": False}
    plan = next(
        t for t in c._tools(None, turn=c.turn) if t["name"] == "plan_ground_mission"
    )
    assert plan is llm_planner.PLAN_TOOL_FD
    assert plan["input_schema"]["properties"]["engine"]["enum"] == ["fast_downward"]
    c.turn.mission = {"omniplanner": True, "grstaps": True}
    plan = next(
        t for t in c._tools(None, turn=c.turn) if t["name"] == "plan_ground_mission"
    )
    assert plan is llm_planner.PLAN_TOOL


def test_mission_prompt_without_grstaps_does_not_point_at_it():
    text = llm_planner.context_text(
        {"omniplanner": True, "grstaps": False, "engine": "fast_downward"}
    )
    assert "picked the Fast Downward planner" in text
    assert "GRSTAPS-X" not in text.split("picked the Fast Downward planner")[1]
    with_gr = llm_planner.context_text(
        {"omniplanner": True, "grstaps": True, "engine": "fast_downward"}
    )
    assert "needs GRSTAPS-X" in with_gr


@pytest.fixture
def fd_only_prompts(monkeypatch):
    """The real heracles_agents.prompts after GRSTAPS-X's removal: ENGINES is
    ("fast_downward",), so repair_goal_prompt("grstaps") raises."""
    prompts = pytest.importorskip("heracles_agents.prompts")
    monkeypatch.setattr(prompts, "ENGINES", ("fast_downward",))
    with pytest.raises(ValueError):
        prompts.repair_goal_prompt("grstaps")
    return prompts


def test_chat_goals_work_when_heracles_agents_has_no_grstaps(fd_only_prompts):
    c = _running_grstaps(
        chat()
    )  # even with a GRSTAPS-X planner up (and goal_manager on it)
    c.goal_check = lambda goal, constraints_json, conf: ""
    c.turn.engine = "grstaps"  # a stale toggle
    system = c._system(None, "euclid", turn=c.turn)
    assert (
        fd_only_prompts.repair_goal_prompt("fast_downward", names=GOAL_PROMPT_NAMES)
        in system
    )
    assert "The rules above are for fast_downward" in system
    assert "engine='grstaps'" not in system
    c.turn.engine = (
        None  # goal_manager forwards to GRSTAPS-X: still Fast Downward's rules
    )
    assert "The rules above are for fast_downward" in c._system(
        None, "euclid", turn=c.turn
    )
    # the model asks for GRSTAPS-X anyway: the card plans with Fast Downward
    c.turn.pddl_goals = []
    [card], out = c._run_tool("propose_goal", {"kind": "pddl", "pddl_goal": "(visited-object o2)", "engine": "grstaps"},
                              "", "euclid", None, None, turn=c.turn)  # fmt: skip
    assert card["body"]["engine"] == "fast_downward" and out["proposed"]


def test_stream_turn_without_grstaps(fd_only_prompts):
    c = _running_grstaps(chat())
    seen = []
    c.llm_client, c._client_provider = (lambda: object()), "anthropic"

    def llm(client, message, robot, src, history, model, turn):
        seen.append(turn)
        yield from ()

    c._llm_anthropic = llm
    events = list(c.stream("hello", "euclid", src="file:none", engine="grstaps"))
    assert not [e for e in events if e.get("type") == "error"], events
    [turn] = seen
    assert turn.engine == "fast_downward" and turn.grstaps is False


def test_a_chat_without_heracles_agents_is_fast_downward_only():
    c = chat()
    c.goal_prompts = None
    assert [e["id"] for e in _running_grstaps(c).chat_engines()] == ["fast_downward"]
    assert "PDDL GOALS ARE OFF" in c._goal_text(cypher=False, turn=c.turn)


# ------------------------------------------------------------------ ROS bridge
def test_no_grstaps_says_not_installed_and_names_no_session():
    assert NO_GRSTAPS.startswith("GRSTAPS-X is not installed")
    assert "session" not in NO_GRSTAPS and "base_station_web" not in NO_GRSTAPS


class _Node:
    def __init__(self):
        self.subs = []

    def create_subscription(self, cls, topic, cb, qos):
        self.subs.append((cls, topic))
        return object()


@needs_rclpy
def test_robot_subscriptions_without_task_schedule_msg(monkeypatch):
    """omniplanner_msgs without TaskScheduleMsg: the robot is still set up, with
    no schedule subscription (and so no schedule view)."""

    def import_type(path):
        if path.endswith("TaskScheduleMsg"):
            raise AttributeError(
                "module 'omniplanner_msgs.msg' has no attribute 'TaskScheduleMsg'"
            )
        return path

    monkeypatch.setattr(rb_mod, "import_type", import_type)
    rb = RosBridge(types.SimpleNamespace(ros=False, planner_ns="willow"),
                   fleet=types.SimpleNamespace(topo={"machines": {"hamilton": {"role": "robot"}}}))  # fmt: skip
    rb.node = _Node()
    rb._subscribe_robot("hamilton")
    topics = [t for _, t in rb.node.subs]
    assert "/hamilton/omniplanner_node/compiled_plan_out" in topics
    assert "/willow/omniplanner_node/task_schedule" not in topics
    assert rb._plan["hamilton"].to_dict()["schedule"] is None


def test_fd_goal_routing_ignores_a_missing_grstaps(monkeypatch):
    """No GRSTAPS-X anywhere: a Fast Downward goal goes through goal_manager as before."""
    rb = RosBridge(types.SimpleNamespace(ros=False, planner_ns="willow"))
    rb.plugins = [p for p in rb.plugins if p["kind"] != "grstaps"]
    sent = []
    monkeypatch.setattr(
        rb,
        "publish",
        lambda topic, msg_type, fields, match_timeout=2.0: sent.append(topic) or 1,
    )
    monkeypatch.setattr(rb, "goal_manager_subscribers", lambda: 1)
    monkeypatch.setattr(rb, "goal_manager_engine", lambda: None)
    for engine in (None, "fast_downward"):
        r = rb.send_pddl_goal("euclid", "(visited-object o1)", engine=engine)
        assert r["via"] == "goal_manager"
    with pytest.raises(LookupError, match="GRSTAPS-X is not installed"):
        rb.send_pddl_goal(
            "euclid", "(visited-object o1)", engine="grstaps", route="planner"
        )
