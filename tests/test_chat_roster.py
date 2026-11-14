"""The chat's system context lists the fleet roster (robot -> platform, arm,
online) and the selected robot, so the model never guesses a platform."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml

from adt4_console.chat import ChatService, ChatTurn, fleet_roster
from adt4_console.goal_tracker import GoalTracker
from adt4_console.ros_bridge import RosBridge

HERE = Path(__file__).parent
# ADT4_TOPOLOGY (e.g. the superproject's network_diagnostics/config/topology.yaml),
# else the repo's test copy of it
TOPOLOGIES = [Path(p) for p in [os.environ.get("ADT4_TOPOLOGY")] if p] + [
    HERE / "fixtures" / "topology.yaml"
]


def topology() -> dict:
    for p in TOPOLOGIES:
        if p.exists():
            return yaml.safe_load(p.read_text())
    pytest.skip("no topology.yaml")


class Fleet:
    def __init__(self, topo):
        self.topo = topo

    def robots(self):
        return sorted(
            n for n, m in self.topo["machines"].items() if m.get("role") == "robot"
        )


class Ros:
    def __init__(self, topo):
        self.fleet = Fleet(topo)
        self.goals = GoalTracker()

    def platform(self, robot):
        return RosBridge.platform(self, robot)


class Live:
    def robots(self):
        return ["euclid", "simbot"]

    def links(self):
        return {
            "robots": {"euclid": {"connected": True}, "simbot": {"connected": False}}
        }

    def cameras(self, robot):
        return ["frontleft", "hand_color_image"] if robot == "euclid" else []


def chat(live=None):
    c = ChatService(NS(llm=False), None, live, Ros(topology()))
    c.turn = (
        ChatTurn()
    )  # the turn the tests' calls belong to (ChatService.stream makes one per turn)
    return c


def test_system_context_lists_euclid_as_a_spot_with_an_arm():
    c = chat()
    text = c._system(None, "euclid")
    assert "Fleet roster" in text
    line = next(x for x in text.splitlines() if x.startswith("- euclid:"))
    assert "Spot" in line and "with arm" in line and "picks up" in line
    assert "(selected)" in line
    assert "Selected robot: euclid" in text


def test_husky_has_no_arm_and_live_state_is_shown():
    topo = topology()
    rows = {r["robot"]: r for r in fleet_roster(Ros(topo), Live())}
    assert rows["euclid"]["platform"] == "spot" and rows["euclid"]["arm"] is True
    assert rows["euclid"]["online"] is True and "hand camera" in rows["euclid"]["label"]
    huskies = [r for r in rows.values() if r["platform"] == "phoenix"]
    assert huskies and all(
        "NO arm" in r["label"] and r["arm"] is False for r in huskies
    )
    assert rows["simbot"]["online"] is False  # live only, not in the topology


def test_sent_goal_status_reaches_the_context():
    c = chat()
    gid = c.ros.goals.goal_sent("euclid", kind="pddl", summary="(holding euclid o1)")
    c.ros.goals.on_plan("euclid", {"plan_id": "abc", "actions": [{"type": "PICK"}]})
    text = c._system(None, "euclid")
    assert gid in text and "(holding euclid o1)" in text


def test_goal_tool_documents_pick_only_and_prefers_pddl():
    desc = ChatService._T_PROPOSE_GOAL["description"]
    assert "(holding euclid o1)" in desc
    assert "only for what PDDL cannot express" in desc


def test_map_first_then_images_and_platform_is_not_a_robot_name():
    from adt4_console import cypher_tools

    assert "look in the map FIRST" in cypher_tools.SYSTEM
    assert "search_robot_images" in cypher_tools.SYSTEM
    assert (
        "is a \\\nplatform, not a robot name" not in cypher_tools.SYSTEM
    )  # one line in the prompt
    assert "platform, not a robot name" in cypher_tools.SYSTEM


def test_mission_planner_chat_leaves_the_live_fleet_out():
    """The mission planner (sim_only) plans for the mission's agents: no fleet
    roster (live robots, what they hold) in its context."""
    c = chat()
    ctx = {"roster": [{"id": "spot1", "kind": "spot", "start": [0.0, 0.0], "binding": None}],
           "areas": [], "no_go": [], "planted": [], "mission": None}  # fmt: skip
    c.turn = ChatTurn(mission={**ctx, "sim_only": True})
    assert c._sim_only(c.turn)
    assert "Fleet roster" not in c._system(None, None, c.turn)
    # the Robot view's mission chat still sees the fleet
    c.turn = ChatTurn(mission=ctx)
    assert not c._sim_only(c.turn)
    assert "Fleet roster" in c._system(None, None, c.turn)
