"""Team goals (plan repair v5): one PDDL goal covers the whole team, the planner
(multi-robot Fast Downward / GRSTAPS-X) allocates the work, and the console
sends it once. The rules are heracles_agents' (heracles_agents.prompts); the live
transcript of 2026-11-11 (two per-robot goals, the second preempting the
first) is the regression case."""

from __future__ import annotations

import types

from adt4_console.chat import ChatService, goal_atoms
from adt4_console.ros_bridge import (
    COMMANDED_GOAL_TYPE,
    RosBridge,
    goal_named_robots,
    is_team_goal,
    normalize_constraints,
)
from tests.conftest import _has_grstaps_prompts, needs_heracles_agents
from tests.test_chat_roster import chat
from tests.test_goal_tracker import make, plan

CHAIRS = ["o2", "o8", "o11", "o15", "o19", "o23", "o27"]
ALL_CHAIRS = "(and " + " ".join(f"(visited-object {o})" for o in CHAIRS) + ")"
BOTH = [{"predicate": "use-robots", "symbols": ["euclid", "hamilton"]}]


# ------------------------------------------------------------------ prompt
# The goal rules themselves are heracles_agents' (test_chat_goal_prompts.py);
# without it PDDL goals are off, so these tests need it.
@needs_heracles_agents
def test_prompt_has_the_team_goal_rules():
    text = chat()._system(None, "euclid")
    for line in (
        "One goal per request covers the whole team",
        "a new goal replaces the whole team's current plan",
        "Never send one goal per robot",
        "the goal MUST name that robot",
        "not the nearest robot",
        "Never split the targets between robots yourself",
        "{predicate: 'use-robots', symbols: ['euclid', 'hamilton']}",
        '"only X"',
        "(object-in-place o t)",
        "re-send the tasks of the last goal that are still wanted",
    ):
        assert line in text, line
    # GRSTAPS-X is optional: named only when heracles_agents has its goal rules
    assert ("engine='grstaps'" in text) == _has_grstaps_prompts()
    assert "Visit all the chairs with both euclid and hamilton" in text
    assert "NOT two goals, one per robot" in text
    assert "Split the 4 cones among both" in text
    assert "(or (and (holding hamilton o1)" in text and "(holding euclid o20))" in text
    # the old, wrong advice is gone from the tool
    desc = ChatService._T_PROPOSE_GOAL["description"]
    assert "whichever robot is closer" not in desc
    assert "WHOLE TEAM" in desc and "never one goal per robot" in desc
    assert "use-robots" in desc
    enum = ChatService._T_PROPOSE_GOAL["input_schema"]["properties"]["constraints"]["items"]["properties"]["predicate"]["enum"]
    assert "use-robots" in enum


@needs_heracles_agents
def test_system_prompt_includes_team_rules_with_and_without_neo4j():
    c = chat()
    assert "One goal per request covers the whole team" in c._system(None, "euclid", turn=c.turn)


# --------------------------------------------------------------- proposals
def _turn(c):
    c.turn.pddl_goals = []  # what ChatService.stream does per turn


@needs_heracles_agents
def test_both_robots_is_one_team_card():
    c = chat()
    _turn(c)
    events, out = c._run_tool(
        "propose_goal",
        {"kind": "pddl", "pddl_goal": ALL_CHAIRS, "constraints": BOTH},
        "Visit all the chairs with both euclid and hamilton", "euclid", None, None, turn=c.turn
    )
    [card] = events
    assert card["team"] == ["euclid", "hamilton"] and card["robot"] == "euclid"
    assert card["body"]["constraints"] == BOTH and "replaces" not in card
    assert out["proposed"] and out["team"] == ["euclid", "hamilton"]


@needs_heracles_agents
def test_team_card_goes_through_a_team_robot_not_the_selected_one():
    c = chat()
    _turn(c)
    [card], _ = c._run_tool("propose_goal", {"kind": "pddl", "pddl_goal": ALL_CHAIRS, "constraints": BOTH},
                            "", "gauss", None, None, turn=c.turn)  # fmt: skip
    assert card["robot"] == "euclid"  # gauss is not allowed to act


@needs_heracles_agents
def test_robot_free_goal_without_use_robots_lists_the_fleet():
    c = chat()
    _turn(c)
    [card], _ = c._run_tool("propose_goal", {"kind": "pddl", "pddl_goal": "(visited-object o2)"},
                            "", "euclid", None, None, turn=c.turn)  # fmt: skip
    assert {"euclid", "hamilton"} <= set(card["team"]) and card["robot"] == "euclid"


@needs_heracles_agents
def test_single_named_robot_is_not_a_team_card():
    c = chat()
    _turn(c)
    [card], _ = c._run_tool("propose_goal", {"kind": "pddl", "pddl_goal": "(holding hamilton o1)"},
                            "", "euclid", None, None, turn=c.turn)  # fmt: skip
    assert "team" not in card and card["robot"] == "hamilton"


@needs_heracles_agents
def test_second_per_robot_goal_in_one_reply_is_refused():
    """The live failure: one goal per robot, the second send preempting the first."""
    c = chat()
    _turn(c)
    per_robot = lambda r: "(and " + " ".join(f"(visited-object-by {r} {o})" for o in CHAIRS) + ")"  # noqa: E731
    first, _ = c._run_tool("propose_goal", {"kind": "pddl", "pddl_goal": per_robot("euclid")}, "", "euclid", None, None, turn=c.turn)
    assert first
    events, out = c._run_tool("propose_goal", {"kind": "pddl", "pddl_goal": per_robot("hamilton")}, "", "euclid", None,
                              None, turn=c.turn)  # fmt: skip
    assert events == [] and out["error"].startswith("NOT PROPOSED")
    assert "One goal covers the whole team" in out["error"]
    # the same for two picks sent apart
    _turn(c)
    c._run_tool("propose_goal", {"kind": "pddl", "pddl_goal": "(holding hamilton o1)"}, "", "euclid", None, None, turn=c.turn)
    events, out = c._run_tool("propose_goal", {"kind": "pddl", "pddl_goal": "(holding euclid o20)"}, "", "euclid",
                              None, None, turn=c.turn)  # fmt: skip
    assert events == [] and "(holding hamilton o1)" in out["error"]


@needs_heracles_agents
def test_combined_goal_replaces_the_earlier_proposal():
    c = chat()
    _turn(c)
    c._run_tool("propose_goal", {"kind": "pddl", "pddl_goal": "(holding hamilton o1)"}, "", "euclid", None, None, turn=c.turn)
    [card], out = c._run_tool("propose_goal", {"kind": "pddl", "pddl_goal": "(and (holding hamilton o1) (holding euclid o20))"},
                              "", "euclid", None, None, turn=c.turn)  # fmt: skip
    assert card["replaces"] is True and card["team"] == ["hamilton", "euclid"]
    assert "replaces" in out["note"]


@needs_heracles_agents
def test_a_new_turn_starts_clean():
    c = chat()
    _turn(c)
    c._run_tool("propose_goal", {"kind": "pddl", "pddl_goal": "(visited-object o2)"}, "", "euclid", None, None, turn=c.turn)
    _turn(c)
    [card], _ = c._run_tool("propose_goal", {"kind": "pddl", "pddl_goal": "(visited-object o9)"}, "", "euclid", None,
                            None, turn=c.turn)  # fmt: skip
    assert "replaces" not in card


def test_stream_starts_a_new_turn():
    from tests.test_chat_goal_prompts import _turns_of

    c = chat()
    turns = _turns_of(c)
    list(c.stream("hello", "euclid", src="file:none"))
    turns[0].pddl_goals.append("(visited-object o2)")
    list(c.stream("hello", "euclid", src="file:none"))
    assert turns[1] is not turns[0] and turns[1].pddl_goals == []


def test_goal_atoms_and_robot_parsing():
    assert goal_atoms("(and (visited-object O2)(holding euclid o1))") == {"(visited-object o2)", "(holding euclid o1)"}
    assert goal_named_robots("(and (visited-object-by hamilton o2) (holding euclid o1) (at-place gauss t4))") == [
        "hamilton", "euclid", "gauss"]
    assert is_team_goal(ALL_CHAIRS)
    assert is_team_goal("(visited-object-by euclid o1)", BOTH)
    assert is_team_goal("(and (holding hamilton o1) (holding euclid o20))")
    assert not is_team_goal("(holding euclid o1)")
    assert not is_team_goal("(visited-object-by hamilton o2)")


# ------------------------------------------------------------------- send
def _bridge(monkeypatch, gm_subs=1):
    rb = RosBridge(types.SimpleNamespace(ros=False, planner_ns="willow"))
    sent = []

    def publish(topic, msg_type, fields, match_timeout=2.0):
        sent.append((topic, msg_type, fields))
        return 1

    monkeypatch.setattr(rb, "publish", publish)
    monkeypatch.setattr(rb, "goal_manager_subscribers", lambda: gm_subs)

    return rb, sent


def test_use_robots_constraint_normalizes():
    assert normalize_constraints([["use-robots", "Euclid", "hamilton"]]) == [
        {"predicate": "use-robots", "symbols": ["euclid", "hamilton"]}]
    import pytest

    with pytest.raises(ValueError):
        normalize_constraints([["use-robots"]])


def test_team_goal_is_published_once_with_use_robots(monkeypatch):
    rb, sent = _bridge(monkeypatch)
    r = rb.send_pddl_goal("euclid", ALL_CHAIRS, constraints=BOTH)
    assert len(sent) == 1
    topic, msg_type, fields = sent[0]
    assert topic == "/willow/commanded_goal" and msg_type == COMMANDED_GOAL_TYPE
    assert fields["goal"] == {"robot_id": "euclid", "pddl_goal": ALL_CHAIRS}
    assert fields["constraints"] == BOTH
    assert r["via"] == "goal_manager"
    # use-robots is this goal's: never a standing constraint of the robot
    assert rb.constraints("euclid") == []
    rb.send_pddl_goal("euclid", "(visited-object o2)")
    assert sent[-1][2]["constraints"] == []
    # the tracker follows both robots of the team goal
    v = rb.goals.view(r["track"])
    assert v["team"] is True and set(v["robots"]) == {"euclid", "hamilton"}


def test_standing_constraints_still_join_a_team_goal(monkeypatch):
    rb, sent = _bridge(monkeypatch)
    rb.send_pddl_goal("euclid", ALL_CHAIRS, constraints=[["forbidden-poi", "o14"], *BOTH])
    assert sent[-1][2]["constraints"] == [{"predicate": "forbidden-poi", "symbols": ["o14"]}, *BOTH]
    assert rb.constraints("euclid") == [{"predicate": "forbidden-poi", "symbols": ["o14"]}]


def test_use_robots_is_not_a_standing_constraint(monkeypatch):
    import pytest

    rb, _ = _bridge(monkeypatch)
    with pytest.raises(ValueError):
        rb.add_constraints("euclid", BOTH)


def test_combined_pick_goal_names_both_robots(monkeypatch):
    rb, sent = _bridge(monkeypatch)
    rb.send_pddl_goal("hamilton", "(and (holding hamilton o1) (holding euclid o20))")
    assert len(sent) == 1
    assert rb.goal_robots("hamilton", sent[0][2]) == ["hamilton", "euclid"]


def test_grstaps_takes_use_robots_without_a_warning(monkeypatch):
    """GRSTAPS-X applies use-robots (omniplanner c07d67a: the robots it leaves out
    get empty plans), so the goal carries it and nothing says otherwise."""
    rb, sent = _bridge(monkeypatch)
    monkeypatch.setattr(rb, "goal_manager_engine", lambda: "grstaps")
    r = rb.send_pddl_goal("euclid", ALL_CHAIRS, constraints=BOTH, engine="grstaps")
    assert len(sent) == 1 and sent[0][2]["constraints"] == BOTH
    assert r["via"] == "goal_manager"
    assert "use-robots" not in (r.get("warning") or "")
    # straight to the GRSTAPS-X plugin (no goal_manager): the same
    rb, sent = _bridge(monkeypatch, gm_subs=0)
    monkeypatch.setattr(rb, "grstaps_plugin", lambda: {"name": "grstaps_planner", "topic": "/willow/g",
                                                       "msg_type": "omniplanner_msgs/msg/ConstrainedPddlGoalMsg"})
    r = rb.send_pddl_goal("euclid", ALL_CHAIRS, constraints=BOTH, engine="grstaps")
    assert len(sent) == 1 and sent[0][2]["constraints"] == BOTH
    assert r["via"] == "planner" and "sent straight to GRSTAPS-X" in r["warning"]
    assert "use-robots" not in r["warning"]


# ---------------------------------------------------------------- tracker
def test_tracker_gives_a_team_goal_the_plan_of_a_robot_it_did_not_name():
    """The planner gave all the chairs to hamilton; the goal went through euclid."""
    tr, clock, _ = make()
    gid = tr.goal_sent("euclid", robots=["euclid"], kind="pddl", team=True)
    clock.step()
    tr.on_plan("hamilton", plan("ph", "FOLLOW", "FOLLOW"))
    tr.on_plan("euclid", plan("pe"))
    tr.on_plan_status({"robot": "euclid", "ok": True})
    v = tr.view(gid)
    assert v["robots"]["hamilton"]["plan_id"] == "ph" and v["robots"]["hamilton"]["named"] is False
    assert v["robots"]["euclid"]["stage"] == "done"  # empty plan: nothing to do


def test_tracker_without_team_flag_keeps_the_old_matching():
    tr, clock, _ = make()
    gid = tr.goal_sent("euclid", robots=["euclid"], kind="pddl")
    clock.step()
    tr.on_plan("hamilton", plan("ph", "FOLLOW"))  # before any plan of the goal: not claimed
    assert "hamilton" not in tr.view(gid)["robots"]

