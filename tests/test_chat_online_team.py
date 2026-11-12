"""The chat's team is the robots online now (2026-11-11: euclid and hamilton
running, gauss in the topology but not connected; "all three robots" gave
gauss tasks and the card said "Send to team (euclid, gauss, hamilton)")."""

from __future__ import annotations

from adt4_console.chat import ChatService, ChatTurn, fleet_roster, operator_named_robots
from tests.conftest import needs_heracles_agents
from tests.test_chat_roster import Ros, topology

THREE = "(and (visited-object-by euclid o29) (visited-object-by gauss o30) (visited-object-by hamilton o31))"
TWO = "(and (visited-object-by euclid o29) (visited-object-by hamilton o31))"


class Live:
    """fleetlink: euclid and hamilton connected; gauss never connected."""

    def __init__(self, links=None):
        self._links = links if links is not None else {"euclid": {"connected": True}, "hamilton": {"connected": True}}

    def robots(self):
        return list(self._links)

    def links(self):
        return {"robots": self._links}

    def cameras(self, robot):
        return []

    def state(self, robot):
        return None


def chat(live=None):
    from types import SimpleNamespace as NS

    c = ChatService(NS(llm=False), None, live or Live(), Ros(topology()))
    c.turn = ChatTurn()  # a new turn
    return c


def propose(c, goal, message="", constraints=None, selected="euclid"):
    args = {"kind": "pddl", "pddl_goal": goal}
    if constraints:
        args["constraints"] = constraints
    return c._run_tool("propose_goal", args, message, selected, None, None, turn=c.turn)


def test_roster_marks_gauss_offline_and_the_team_is_the_online_robots():
    rows = {r["robot"]: r for r in fleet_roster(Ros(topology()), Live())}
    assert rows["euclid"]["online"] is True and rows["hamilton"]["online"] is True
    assert rows["gauss"]["online"] is False  # in the topology, never connected
    text = chat()._system(None, "euclid")
    team_line = next(x for x in text.splitlines() if x.startswith("The team now"))
    assert "online, connected): euclid, hamilton." in team_line
    assert "Offline — do not assign: " in team_line and "gauss" in team_line.split("do not assign:")[1]
    assert '"All robots", "the team", "both robots"' in team_line
    gauss = next(x for x in text.splitlines() if x.startswith("- gauss:"))
    assert gauss.endswith("; offline")


def test_without_link_state_nothing_is_called_offline():
    rows = fleet_roster(Ros(topology()), Live(links={}))
    assert all(r["online"] is None for r in rows)
    assert "The team now" not in chat(Live(links={}))._system(None, "euclid")


def test_operator_named_robots_matches_whole_words():
    assert operator_named_robots("send Gauss and euclid", {"gauss", "hamilton"}) == {"gauss"}
    assert operator_named_robots("gaussian blur", {"gauss"}) == set()


@needs_heracles_agents
def test_goal_naming_an_offline_robot_is_not_proposed():
    c = chat()
    events, out = propose(c, THREE, "replan all three robots")
    assert events == []
    assert out["error"].startswith("NOT PROPOSED: gauss is offline (not connected); online robots: euclid, hamilton")
    # use-robots naming it too
    events, out = propose(c, "(visited-object o2)", "visit o2 with the team",
                          constraints=[{"predicate": "use-robots", "symbols": ["euclid", "gauss"]}])  # fmt: skip
    assert events == [] and "gauss is offline" in out["error"]


@needs_heracles_agents
def test_online_team_card_and_send_label_have_only_online_robots():
    c = chat()
    [card], out = propose(c, "(visited-object o2)", "replan the team")
    assert card["team"] == ["euclid", "hamilton"] and "warning" not in card
    c = chat()
    [card], out = propose(c, TWO, "both robots")
    assert card["team"] == ["euclid", "hamilton"] and out["proposed"]


@needs_heracles_agents
def test_operator_naming_the_offline_robot_gets_a_warning_card():
    c = chat()
    [card], out = propose(c, THREE, "send euclid, gauss and hamilton to the cones", selected="gauss")
    assert card["warning"].startswith("gauss is offline")
    assert out["warning"] == card["warning"]
    assert card["team"] == ["euclid", "hamilton"]  # the card's team / send label: online only
    assert card["robot"] == "euclid"  # never sent through the offline robot
