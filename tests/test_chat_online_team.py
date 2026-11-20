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
        self._links = (
            links
            if links is not None
            else {"euclid": {"connected": True}, "hamilton": {"connected": True}}
        )

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
    assert "(online, or link stale): euclid, hamilton." in team_line
    assert (
        "Offline — do not assign: " in team_line
        and "gauss" in team_line.split("do not assign:")[1]
    )
    assert '"All robots", "the team", "both robots"' in team_line
    gauss = next(x for x in text.splitlines() if x.startswith("- gauss:"))
    assert gauss.endswith("; offline")


def test_without_link_state_nothing_is_called_offline():
    rows = fleet_roster(Ros(topology()), Live(links={}))
    assert all(r["online"] is None for r in rows)
    assert "The team now" not in chat(Live(links={}))._system(None, "euclid")


def test_operator_named_robots_matches_whole_words():
    assert operator_named_robots("send Gauss and euclid", {"gauss", "hamilton"}) == {
        "gauss"
    }
    assert operator_named_robots("gaussian blur", {"gauss"}) == set()


@needs_heracles_agents
def test_goal_naming_an_offline_robot_is_not_proposed():
    c = chat()
    events, out = propose(c, THREE, "replan all three robots")
    assert events == []
    assert out["error"].startswith(
        "NOT PROPOSED: gauss is offline (not connected); online robots: euclid, hamilton"
    )
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
    [card], out = propose(
        c, THREE, "send euclid, gauss and hamilton to the cones", selected="gauss"
    )
    assert card["warning"].startswith("gauss is offline")
    assert out["warning"] == card["warning"]
    assert card["team"] == [
        "euclid",
        "hamilton",
    ]  # the card's team / send label: online only
    assert card["robot"] == "euclid"  # never sent through the offline robot


def test_the_roster_is_the_one_robot_list_and_offline_is_not_unknown():
    """2026-11-11 rig: "visit O4 and O8 using only the spots" made the model say
    apollo "is not in the fleet": the roster had six robots, the database three
    Robot nodes, and the rules pointed at the database."""
    text = chat()._system(None, "euclid")
    head = next(x for x in text.splitlines() if x.startswith("Fleet roster"))
    assert "This roster is the fleet, the one list of robots" in head
    assert "even with no Robot node in the database" in head
    team_line = next(x for x in text.splitlines() if x.startswith("The team now"))
    assert (
        '("the spots", "the huskies") the online robots of that platform' in team_line
    )
    assert "An offline robot is a known robot: say it is offline" in team_line
    assert "never that it is unknown or not in the fleet" in team_line


@needs_heracles_agents
def test_the_rules_point_at_the_roster_not_at_the_database_for_who_is_in_the_fleet():
    text = chat()._system(None, "euclid")
    assert (
        "The fleet is ONE list: the fleet roster in this prompt when it has one" in text
    )
    assert "are the robots of the fleet (MATCH (r:Robot)" not in text
    assert "Use only robots of the fleet (MATCH (r:Robot)" not in text


@needs_heracles_agents
def test_two_by_atoms_for_one_target_make_one_card_for_both_robots():
    """ "hamilton and euclid go to O31": each robot its own -by atom, no use-robots."""
    c = chat()
    goal = "(and (visited-object-by hamilton o31) (visited-object-by euclid o31))"
    [card], out = propose(c, goal, "hamilton and euclid go to O31")
    assert card["body"]["goal"] == goal and not card["body"].get("constraints")
    assert sorted(card["team"]) == ["euclid", "hamilton"] and out["proposed"]


@needs_heracles_agents
def test_all_robots_without_use_robots_keeps_the_offline_husky_off_the_team():
    """ "all robots visit the trash": a robot-free goal and no use-robots; the
    card's team is the online robots, and gauss (offline husky) is not on it."""
    c = chat()
    [card], out = propose(
        c, "(and (visited-object o4) (visited-object o8))", "all robots visit the trash"
    )
    assert not card["body"].get("constraints")
    assert "gauss" not in card["team"] and card["team"] == ["euclid", "hamilton"]


def test_the_roster_says_what_each_robot_holds():
    """willow, 2026-11-12: hamilton held O58 and was told to go to a place; the
    goal did not name O58, so the planner put it down first. The chat can only
    ask about it if it knows who holds what."""
    from types import SimpleNamespace as NS

    c = chat()
    roster = [
        x
        for x in c._system(None, "euclid").splitlines()
        if x.startswith("- ") and ": Spot" in x
    ]
    assert roster and not any(
        "; HOLDING" in x for x in roster
    )  # no planner state: nothing claimed
    c.ros.planner_state = NS(holding=lambda: {"hamilton": ["O58"]})
    text = c._system(None, "euclid")
    hamilton = next(x for x in text.splitlines() if x.startswith("- hamilton:"))
    assert hamilton.endswith("; HOLDING O58")
    euclid = next(x for x in text.splitlines() if x.startswith("- euclid:"))
    assert "HOLDING" not in euclid


def test_planner_state_holding_never_raises():
    from adt4_console.planner_state import PlannerState

    class Maps:
        def target(self):
            raise LookupError("no Neo4j target")

    assert PlannerState(None, None, Maps()).holding() == {}
    rows = [{"robot": "hamilton", "object": "O58"}]
    src = type(
        "S", (), {"id": "live", "query": lambda self, q: rows if "HOLDS" in q else []}
    )()
    assert PlannerState(
        None, None, type("M", (), {"target": lambda self: src})()
    ).holding() == {"hamilton": ["O58"]}


@needs_heracles_agents
def test_a_goal_that_does_not_name_a_held_object_gets_a_warning_on_the_card():
    from types import SimpleNamespace as NS

    def held(c):
        c.ros.planner_state = NS(holding=lambda: {"hamilton": ["O58"]})
        return c

    # hamilton holds O58 and is only told to go somewhere: warned
    [card], out = propose(
        held(chat()), "(visited-object-by hamilton o31)", "hamilton go to O31"
    )
    assert card["warning"].startswith(
        "hamilton is holding O58: this goal does not name it"
    )
    assert (
        "put down where the robot stands" in card["warning"]
        and out["warning"] == card["warning"]
    )
    # the goal carries it: no warning
    [card], out = propose(
        held(chat()), "(object-placed-by hamilton o58 t7)", "hamilton, take it to t7"
    )
    assert "warning" not in card and "warning" not in out
    [card], _ = propose(
        held(chat()),
        "(and (holding hamilton o58) (visited-object-by hamilton o31))",
        "",
    )
    assert "warning" not in card
    # a goal for another robot leaves hamilton and its object alone: no warning
    [card], _ = propose(
        held(chat()), "(visited-object-by euclid o31)", "euclid go to O31"
    )
    assert "warning" not in card
    # a team goal may take any robot: warned
    [card], _ = propose(held(chat()), "(visited-object o31)", "someone visit O31")
    assert "hamilton is holding O58" in card["warning"]
    # nobody holds anything: no warning
    [card], _ = propose(
        chat(), "(visited-object-by hamilton o31)", "hamilton go to O31"
    )
    assert "warning" not in card


# ---------- a goal naming no robot while robots are executing ----------


def executing(c, *robots):
    from types import SimpleNamespace as NS

    c.ros.goals = NS(
        busy_robots=lambda: {r: "(visited-object-by %s o1)" % r for r in robots},
        summary_lines=lambda n=3: [],
    )
    return c


ALL_ONLINE = {
    "euclid": {"connected": True},
    "hamilton": {"connected": True},
    "gauss": {"connected": True},
}


@needs_heracles_agents
def test_a_goal_naming_no_robot_while_robots_execute_suggests_the_free_robots():
    c = executing(chat(Live(ALL_ONLINE)), "euclid", "hamilton")
    [card], out = propose(c, "(visited-object o31)", "also go look at O31")
    assert card["warning"] == (
        "euclid and hamilton are executing a goal and this goal has a task naming no "
        "robot: the planner plans it for the whole team and may interrupt them. To "
        "leave them alone, limit it to the free robot: use-robots gauss"
    )
    assert out["warning"] == card["warning"]
    assert out["suggest_to_operator"].startswith(card["warning"])
    assert (
        "Do not propose it again until the operator says so"
        in out["suggest_to_operator"]
    )
    # the goal is still proposed, as asked
    assert out["proposed"] is True and card["body"]["goal"] == "(visited-object o31)"


@needs_heracles_agents
def test_no_suggestion_when_the_goal_already_says_which_robots():
    # a -by task
    c = executing(chat(Live(ALL_ONLINE)), "euclid", "hamilton")
    [card], out = propose(c, "(visited-object-by gauss o31)", "gauss go look at O31")
    assert "warning" not in card and "suggest_to_operator" not in out
    # use-robots
    c = executing(chat(Live(ALL_ONLINE)), "euclid", "hamilton")
    [card], out = propose(
        c,
        "(visited-object o31)",
        "go look at O31",
        constraints=[{"predicate": "use-robots", "symbols": ["gauss"]}],
    )
    assert "warning" not in card and "suggest_to_operator" not in out


@needs_heracles_agents
def test_no_suggestion_when_no_robot_is_executing():
    [card], out = propose(chat(Live(ALL_ONLINE)), "(visited-object o31)", "look at O31")
    assert "warning" not in card and "suggest_to_operator" not in out


@needs_heracles_agents
def test_no_suggestion_when_the_operator_asks_for_the_whole_team():
    for message in (
        "all robots go look at O31",
        "have the whole team visit O31",
        "every spot visits O31",
        "new mission: visit O31",
        "any free robot can visit O31",
    ):
        c = executing(chat(Live(ALL_ONLINE)), "euclid")
        [card], out = propose(c, "(visited-object o31)", message)
        assert "suggest_to_operator" not in out, message


@needs_heracles_agents
def test_with_every_robot_executing_it_asks_to_name_one():
    c = executing(chat(Live(ALL_ONLINE)), "euclid", "hamilton", "gauss")
    [card], out = propose(c, "(visited-object o31)", "also go look at O31")
    assert "No robot is free: name the robot that should do it" in card["warning"]
    assert "use-robots gauss" not in card["warning"]


@needs_heracles_agents
def test_an_offline_robot_is_neither_busy_nor_free():
    # gauss is offline (never connected); only euclid executes
    c = executing(chat(), "euclid", "gauss")
    [card], _ = propose(c, "(visited-object o31)", "also go look at O31")
    assert card["warning"].startswith("euclid is executing a goal")
    assert card["warning"].endswith("limit it to the free robot: use-robots hamilton")


@needs_heracles_agents
def test_a_mixed_goal_with_a_robot_free_task_is_suggested_too():
    c = executing(chat(Live(ALL_ONLINE)), "euclid")
    [card], out = propose(
        c,
        "(and (visited-object-by hamilton o29) (visited-object o31))",
        "hamilton to O29, and someone look at O31",
    )
    assert "suggest_to_operator" in out
    assert card["warning"].endswith("free robots: use-robots gauss hamilton")


@needs_heracles_agents
def test_the_put_down_warning_and_the_suggestion_are_both_on_the_card():
    from types import SimpleNamespace as NS

    c = executing(chat(Live(ALL_ONLINE)), "euclid")
    c.ros.planner_state = NS(holding=lambda: {"hamilton": ["O58"]})
    [card], _ = propose(c, "(visited-object o31)", "also go look at O31")
    assert "hamilton is holding O58" in card["warning"]
    assert "euclid is executing a goal" in card["warning"]


def test_the_roster_says_which_robots_are_executing():
    c = executing(chat(Live(ALL_ONLINE)), "hamilton")
    text = c._system(None, "euclid")
    line = {x.split(":")[0]: x for x in text.splitlines() if x.startswith("- ")}
    assert "EXECUTING a goal now" in line["- hamilton"]
    assert "EXECUTING" not in line["- euclid"] and "EXECUTING" not in line["- gauss"]


@needs_heracles_agents
def test_the_prompt_says_how_to_add_a_task_while_robots_execute():
    """2026-11-18: during a mission the chat never suggested use-robots for an
    added task. Its rules only said to re-send the last goal's tasks with the
    new one, which makes the goal the whole team's."""
    text = executing(chat(Live(ALL_ONLINE)), "hamilton")._system(None, "euclid")
    for line in (
        'ADDING A TASK WHILE ROBOTS EXECUTE (the fleet roster marks them "EXECUTING a goal now")',
        "this replaces the re-send rule above",
        "Propose a goal with only the new task(s), and always with a use-robots constraint",
        "the online robots that are not EXECUTING and can do the task",
        "Do not re-send the tasks of robots the goal does not name",
        "re-send its own tasks that are still wanted with the new one",
        "ask which robot should do it",
        "when the operator adds a task and no robot is EXECUTING, re-send the tasks",
    ):
        assert line in " ".join(text.split()), line
    # the roster line the rule points at is in the same prompt
    assert "- hamilton:" in text and "EXECUTING a goal now" in text


@needs_heracles_agents
def test_an_added_task_limited_to_the_free_robot_is_proposed_without_a_warning():
    c = executing(chat(Live(ALL_ONLINE)), "euclid", "hamilton")
    [card], out = propose(
        c,
        "(visited-object o31)",
        "also go look at O31",
        constraints=[{"predicate": "use-robots", "symbols": ["gauss"]}],
    )
    assert out["proposed"] is True and out["team"] == ["gauss"]
    assert "warning" not in card and "suggest_to_operator" not in out


def test_a_broken_goal_tracker_never_blocks_a_goal():
    from types import SimpleNamespace as NS

    c = chat(Live(ALL_ONLINE))

    def boom():
        raise RuntimeError("tracker gone")

    c.ros.goals = NS(busy_robots=boom, summary_lines=lambda n=3: [])
    assert c._busy() == {}


class NoLinks(Live):
    """No fleetlink state at all; the console has poses from euclid and hamilton."""

    def __init__(self):
        super().__init__(links={})

    def robots(self):
        return ["euclid", "hamilton"]

    def state(self, robot):
        pose = {"x": 1.0, "y": 2.0, "z": 0.0}
        return {"pose": pose} if robot in ("euclid", "hamilton") else None


@needs_heracles_agents
def test_without_link_state_only_robots_with_a_pose_are_called_free():
    """The topology also lists robots that are not out (2026-11-17 rig: the
    suggestion named apollo, lewis and samoyed, and the goal limited to them
    was refused)."""
    c = executing(chat(NoLinks()), "euclid")
    [card], _ = propose(c, "(visited-object o31)", "also go look at O31")
    assert card["warning"].endswith("limit it to the free robot: use-robots hamilton")
