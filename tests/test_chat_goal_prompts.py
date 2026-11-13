"""The chat's PDDL goal knowledge comes from heracles_agents (heracles_agents.prompts),
the same text as its plan-repair chat, and every proposed goal first passes
heracles_agents' goal check (scene_graph_edit.goal_problems). No live LLM: the
model's tool calls are scripted, the goal check runs against a fake Neo4j."""

from __future__ import annotations

import re
from types import SimpleNamespace as NS

import pytest

from adt4_console import chat as chat_mod
from adt4_console.chat import GOAL_PROMPT_NAMES, ChatService
from tests.test_chat_roster import chat

prompts = pytest.importorskip("heracles_agents.prompts")
sge = pytest.importorskip("heracles_agents.scene_graph_edit")

CONES = [f"o{i}" for i in range(1, 16)]
ALL_CONES = "(and " + " ".join(f"(visited-object {o})" for o in CONES) + ")"
BOTH = [{"predicate": "use-robots", "symbols": ["euclid", "hamilton"]}]
ROBOT_FIRST = re.compile(
    r"\((?:visited-object-by|visited-place-by|inspected-by|object-placed-by|holding)\s"
)


class FakeDb:
    """The three queries goal_problems runs, over a fixed map."""

    def __init__(self, robots=("euclid", "hamilton", "gauss"), ids=(), rooms=()):
        self.robots, self.ids, self.rooms = robots, {i.lower() for i in ids}, rooms
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def query(self, q):
        self.queries.append(q)
        if "(r:Robot)" in q:
            return [{"n": r} for r in self.robots]
        if "(g:Room)" in q:
            return [{"s": s} for s in self.rooms]
        asked = re.findall(r"'([^']+)'", q.split(" IN ", 1)[1])
        return [{"s": s} for s in asked if s in self.ids]


@pytest.fixture
def fake_map(monkeypatch):
    db = FakeDb(ids=[*CONES, "o21", "o26", "t11", "o120"])
    monkeypatch.setattr(sge, "_connect", lambda conf: db)
    return db


def neo4j_chat(robot_engine=None):
    c = chat()
    c._cypher_src = lambda src: NS(
        id="neo4j:live", uri="bolt://fake:7687", creds=("u", "p")
    )
    c._schema = NS(text=lambda src: "Database contents now: (fake)")
    c.turn.pddl_goals = []  # a new turn, as ChatService.stream starts one
    return c


def propose(c, goal, constraints=None, selected="euclid"):
    args = {"kind": "pddl", "pddl_goal": goal}
    if constraints:
        args["constraints"] = constraints
    return c._run_tool(
        "propose_goal", args, "", selected, "neo4j:live", None, turn=c.turn
    )


# ------------------------------------------------------------ prompt source
def test_goal_prompt_comes_from_heracles_agents(monkeypatch):
    """Fails if the console keeps its own copy of the goal rules."""
    assert not hasattr(chat_mod, "TEAM_GOALS")
    c = neo4j_chat()
    assert c.goal_prompts is prompts and c.goal_check is sge.goal_problems
    system = c._system("neo4j:live", "euclid", turn=c.turn)
    assert (
        prompts.repair_goal_prompt("fast_downward", names=GOAL_PROMPT_NAMES) in system
    )
    assert prompts.scene_graph_description("hierarchical") in system
    assert prompts.cypher_interface_description() in system
    # what heracles_agents returns is what the model gets, nothing else
    monkeypatch.setattr(
        prompts, "repair_goal_prompt", lambda engine, names=None: f"<<RULES {engine}>>"
    )
    system = c._system("neo4j:live", "euclid", turn=c.turn)
    assert "<<RULES fast_downward>>" in system
    assert "Never pick a robot yourself" not in system  # the rules came only from there
    assert "<<RULES fast_downward>>" in c._system(
        None, "euclid", turn=c.turn
    )  # without Neo4j too


def test_goal_rules_follow_the_goal_managers_engine():
    c = neo4j_chat()
    c.ros.available = True
    c.ros.goal_manager_engine = lambda: "grstaps"
    system = c._system("neo4j:live", "euclid", turn=c.turn)
    assert prompts.repair_goal_prompt("grstaps", names=GOAL_PROMPT_NAMES) in system
    assert "The rules above are for grstaps" in system


def test_prompt_has_the_rules_in_the_consoles_names():
    system = neo4j_chat()._system("neo4j:live", "euclid")
    for rule in (
        "One goal per request covers the whole team",
        "the goal MUST name that robot",
        "use the robot-free forms",
        "Never pick a robot yourself",
        '"Visit all cones" (cones O1 ... O15) is ONE goal',
        "plus a use-robots constraint naming exactly",
        "MeshPlace closest to Y",  # "put X at Y" (heracles_agents #8)
        "X is already at Y: send no goal",
    ):
        assert rule in system, rule
    assert (
        "send_pddl_goal_with_constraints" not in system
        and "pddl_goal_string" not in system
    )
    assert "Using propose_goal with pddl_goal=" in system
    # the old console rules that had the model choose or ask for a robot are gone
    assert "without naming a robot you know, ask" not in system
    assert "then give the task to that robot with a -by predicate" not in system


def test_selected_robot_is_not_a_task_assignment():
    c = neo4j_chat()
    c._robot_pos = lambda robot: (1.0, 2.0, 0.0)
    system = c._system("neo4j:live", "euclid", turn=c.turn)
    line = next(x for x in system.splitlines() if "Selected robot: euclid" in x)
    assert "not a robot assignment for PDDL goals" in line and "point({x: 1.00" in line
    assert (
        "work the operator gives no robot stays robot-free even when a robot is selected"
        in system
    )


def test_without_heracles_agents_pddl_goals_are_off(monkeypatch, caplog):
    """No stale embedded copy: the chat says PDDL goals are off and refuses them."""
    monkeypatch.setattr(
        chat_mod,
        "load_goal_prompts",
        lambda: (None, None, "heracles_agents is not importable (x)"),
    )
    with caplog.at_level("ERROR"):
        c = neo4j_chat()
    assert "PDDL goals are OFF" in caplog.text
    system = c._system("neo4j:live", "euclid", turn=c.turn)
    assert "PDDL GOALS ARE OFF" in system and "TEAM GOALS" not in system
    events, out = propose(c, "(visited-object o1)")
    assert events == [] and out["error"].startswith("NOT PROPOSED: PDDL goals are off")
    # goto still works
    [card], _ = c._run_tool(
        "propose_goal",
        {"kind": "goto", "points": ["O1"]},
        "",
        "euclid",
        None,
        None,
        turn=c.turn,
    )
    assert card["kind"] == "goto"


# ------------------------------------------------------------- proposals
def test_visit_all_cones_is_one_robot_free_goal(fake_map):
    c = neo4j_chat()
    [card], out = propose(c, ALL_CONES)
    assert card["body"] == {"goal": ALL_CONES}
    assert not ROBOT_FIRST.search(card["body"]["goal"])  # no -by, no robot
    assert {"euclid", "hamilton"} <= set(
        card["team"]
    )  # the planner allocates over the fleet
    assert out["proposed"] and "error" not in out
    assert any(
        "toLower(n.nodeSymbol) IN" in q for q in fake_map.queries
    )  # it was checked


def test_named_robots_get_the_by_forms(fake_map):
    goal = "(and (visited-object-by hamilton o21) (inspected-by euclid o26))"
    c = neo4j_chat()
    [card], out = propose(c, goal, selected="gauss")
    assert card["body"]["goal"] == goal
    assert card["team"] == ["hamilton", "euclid"] and card["robot"] == "hamilton"


def test_both_robots_is_robot_free_with_use_robots(fake_map):
    c = neo4j_chat()
    [card], out = propose(c, ALL_CONES, BOTH)
    assert card["body"]["constraints"] == BOTH and not ROBOT_FIRST.search(
        card["body"]["goal"]
    )
    assert card["team"] == ["euclid", "hamilton"]


def test_unknown_robot_is_not_proposed(fake_map):
    c = neo4j_chat()
    events, out = propose(c, "(visited-object-by hamiltonm o21)")
    assert events == []
    assert out["error"].startswith("NOT PROPOSED: unknown robot hamiltonm")
    assert "known robots: euclid, gauss, hamilton" in out["error"]
    # nothing was held for the turn's one-goal guard: a fixed goal is a first proposal
    [card], _ = propose(c, "(visited-object-by hamilton o21)")
    assert "replaces" not in card


def test_unknown_object_and_use_robots_robot_are_not_proposed(fake_map):
    c = neo4j_chat()
    events, out = propose(c, "(and (visited-object o21) (visited-object o999))")
    assert events == [] and "o999 not in the scene graph" in out["error"]
    events, out = propose(
        c, "(visited-object o21)", [{"predicate": "use-robots", "symbols": ["newton"]}]
    )
    assert events == [] and "unknown robot newton" in out["error"]
    events, out = propose(
        c, "(visited-object o21)", [{"predicate": "forbidden-poi", "symbols": ["o404"]}]
    )
    assert events == [] and "o404 not in the scene graph" in out["error"]


def test_goal_check_failure_does_not_block(monkeypatch):
    def down(conf):
        raise OSError("neo4j down")

    monkeypatch.setattr(sge, "_connect", down)
    c = neo4j_chat()
    [card], out = propose(c, "(visited-object o21)")
    assert out["proposed"]


def test_without_neo4j_the_goal_is_not_checked(monkeypatch):
    monkeypatch.setattr(sge, "_connect", lambda conf: pytest.fail("no map to check"))
    c = chat()
    c.turn.pddl_goals = []
    [card], _ = c._run_tool("propose_goal", {"kind": "pddl", "pddl_goal": "(visited-object o2)"},
                            "", "euclid", None, None, turn=c.turn)  # fmt: skip
    assert card["body"]["goal"] == "(visited-object o2)"


def test_image_tools_are_unchanged():
    c = neo4j_chat()
    names = [t["name"] for t in c._tools("neo4j:live", turn=c.turn)]
    assert "search_robot_images" in names and "show_object_images" in names
    assert ChatService._T_SEARCH_IMAGES["input_schema"]["required"] == ["query"]
    assert "look in the map FIRST" in c._system("neo4j:live", "euclid", turn=c.turn)


# ------------------------------------------------------- planner toggle
def test_toggle_engine_sets_the_rules_and_the_proposal(fake_map):
    """The chat's planner toggle (ChatBody.engine) picks the engine's goal rules and
    the engine the proposed goal (and so its card and send) carries."""
    c = neo4j_chat()
    c.turn.engine = "grstaps"  # what stream(engine="grstaps") sets for the turn
    system = c._system("neo4j:live", "euclid", turn=c.turn)
    assert prompts.repair_goal_prompt("grstaps", names=GOAL_PROMPT_NAMES) in system
    assert (
        prompts.repair_goal_prompt("fast_downward", names=GOAL_PROMPT_NAMES)
        not in system
    )
    assert (
        "the planner the operator selected in the chat" in system
        and "engine='grstaps'" in system
    )
    [card], _ = propose(c, "(visited-object o21)")
    assert card["body"]["engine"] == "grstaps"
    # the model's explicit engine still wins
    c.turn.pddl_goals = []
    [card], _ = c._run_tool("propose_goal", {"kind": "pddl", "pddl_goal": "(visited-object o21)", "engine": "fast_downward"},
                            "", "euclid", "neo4j:live", None, turn=c.turn)  # fmt: skip
    assert card["body"]["engine"] == "fast_downward"
    c.turn.engine, c.turn.pddl_goals = "fast_downward", []
    assert prompts.repair_goal_prompt(
        "fast_downward", names=GOAL_PROMPT_NAMES
    ) in c._system("neo4j:live", "euclid", turn=c.turn)
    [card], _ = propose(c, "(visited-object o21)")
    assert card["body"]["engine"] == "fast_downward"


def test_no_toggle_keeps_the_body_as_before(fake_map):
    c = neo4j_chat()
    c.turn.engine = None
    [card], _ = propose(c, "(visited-object o21)")
    assert card["body"] == {"goal": "(visited-object o21)"}


def _turns_of(c):
    """The ChatTurn each ``stream`` call hands the LLM loop."""
    seen = []
    c.llm_client, c._client_provider = (lambda: object()), "anthropic"

    def llm(client, message, robot, src, history, model, turn):
        seen.append(turn)
        yield from ()

    c._llm_anthropic = llm
    return seen


def test_stream_takes_the_toggle_for_the_turn():
    c = chat()
    turns = _turns_of(c)
    list(c.stream("hello", "euclid", src="file:none", engine="grstaps"))
    list(c.stream("hello", "euclid", src="file:none", engine="bogus"))
    assert [t.engine for t in turns] == ["grstaps", None]


def test_grstaps_is_offered_only_when_the_base_runs_it():
    c = chat()
    gr = {e["id"]: e for e in c.chat_engines()}
    assert gr["fast_downward"]["available"] and not gr["grstaps"]["available"]
    assert "ROS is not running" in gr["grstaps"]["reason"]
    c.ros.available = True
    c.ros.goal_manager_engine = lambda: "fast_downward"
    c.ros.find_plugin = lambda kind: None
    gr = {e["id"]: e for e in c.chat_engines()}
    assert (
        not gr["grstaps"]["available"]
        and "no GRSTAPS-X planner is running" in gr["grstaps"]["reason"]
    )
    c.ros.goal_manager_engine = lambda: "grstaps"
    assert {e["id"]: e for e in c.chat_engines()}["grstaps"]["available"]
    assert c.usage()["engines"][1]["id"] == "grstaps"


def test_chat_route_passes_the_toggle(settings, monkeypatch):
    from tests.conftest import make_client

    client, svc = make_client(settings)
    seen = {}

    def stream(*a, **kw):
        seen.update(kw)
        yield {"type": "done"}

    monkeypatch.setattr(svc.chat, "stream", stream)
    with client:
        client.post("/api/chat", json={"message": "hi", "engine": "grstaps"})
        assert seen["engine"] == "grstaps"
        assert (
            client.post(
                "/api/chat", json={"message": "hi", "engine": "nope"}
            ).status_code
            == 422
        )
