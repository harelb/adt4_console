"""GRSTAPS-X availability comes from the live ROS graph, not the console's own
omniplanner config: the console runs with config ``default`` (no Grstaps
plugin) while the planning session runs ``grstaps_repair`` and may start later."""

from __future__ import annotations

import types
from types import SimpleNamespace as NS

import pytest

from adt4_console.chat import ChatService
from adt4_console.ros_bridge import (
    COMMANDED_GOAL_TYPE,
    RosBridge,
    grstaps_topic_candidates,
)

CPG = "omniplanner_msgs/msg/ConstrainedPddlGoalMsg"
GR_TOPIC = "/willow/omniplanner_node/grstaps_planner/pddl_goal"


class FakeNode:
    """A ROS graph: topic -> (types, publisher node names, subscriber count)."""

    def __init__(self, graph=None):
        self.graph = dict(graph or {})
        self.queries = 0

    def get_topic_names_and_types(self):
        self.queries += 1
        return [(t, list(v[0])) for t, v in self.graph.items()]

    def get_publishers_info_by_topic(self, topic):
        return [NS(node_name=n) for n in self.graph.get(topic, ((), (), 0))[1]]

    def count_subscribers(self, topic):
        return self.graph.get(topic, ((), (), 0))[2]


def _chat(rb):
    """A chat on ``rb`` whose heracles_agents has GRSTAPS-X goal rules."""
    c = ChatService(NS(llm=False), None, None, rb)
    c.goal_prompts = NS(ENGINES=("fast_downward", "grstaps"))
    return c


def _bridge(monkeypatch, graph=None, gm_subs=1):
    rb = RosBridge(types.SimpleNamespace(ros=False, planner_ns="willow"))
    # the console's own config (default): no Grstaps plugin
    rb.plugins = [p for p in rb.plugins if p["kind"] != "grstaps"]
    rb.node = FakeNode(graph)
    rb.run = lambda fn, timeout=5.0: fn()
    sent = []

    def publish(topic, msg_type, fields, match_timeout=2.0):
        sent.append((topic, msg_type, fields))
        return 1

    monkeypatch.setattr(rb, "publish", publish)
    monkeypatch.setattr(rb, "goal_manager_subscribers", lambda: gm_subs)
    return rb, sent


def test_candidates_match_grstaps_pddl_goal_topics_only():
    names = [
        (GR_TOPIC, [CPG]),
        ("/other/omniplanner_node/grstaps_planner/pddl_goal", [CPG]),
        (
            "/willow/omniplanner_node/grstaps_planner/grstaps_goal",
            ["omniplanner_msgs/msg/GotoPointsGoalMsg"],
        ),
        ("/willow/omniplanner_node/multi_robot_pddl_constrained/pddl_goal", [CPG]),
        ("/willow/omniplanner_node/grstaps_planner/pddl_goal_x", [CPG]),
    ]
    assert grstaps_topic_candidates(names, "willow") == [
        GR_TOPIC, "/other/omniplanner_node/grstaps_planner/pddl_goal"]  # fmt: skip


def test_goal_manager_forwarding_to_grstaps_is_found_without_the_plugin_in_config(
    monkeypatch,
):
    rb, _ = _bridge(monkeypatch, {GR_TOPIC: ([CPG], ["goal_manager"], 1)})
    assert rb.find_plugin("grstaps") is None  # the console's config has none
    assert rb.grstaps_live() == {
        "topic": GR_TOPIC,
        "goal_manager": True,
        "subscribers": 1,
    }
    assert rb.goal_manager_engine() == "grstaps"


def test_nothing_running_and_stale_names_are_not_grstaps(monkeypatch):
    rb, _ = _bridge(monkeypatch, {})
    assert rb.grstaps_live() is None and rb.goal_manager_engine() is None
    # a topic name left over (our own publisher, no planner behind it)
    rb, _ = _bridge(monkeypatch, {GR_TOPIC: ([CPG], ["adt4_console"], 0)})
    assert rb.grstaps_live() is None


def test_graph_lookup_is_cached_briefly_and_sees_a_planner_started_later(monkeypatch):
    rb, _ = _bridge(monkeypatch, {})
    assert rb.grstaps_live(now=100.0) is None
    rb.node.graph[GR_TOPIC] = (
        [CPG],
        ["goal_manager"],
        1,
    )  # the planning session starts
    assert rb.grstaps_live(now=101.0) is None  # cached
    q = rb.node.queries
    assert rb.grstaps_live(now=104.0)["topic"] == GR_TOPIC  # re-evaluated
    assert rb.node.queries == q + 1


def test_send_grstaps_goes_through_goal_manager_when_it_forwards_there(monkeypatch):
    rb, sent = _bridge(monkeypatch, {GR_TOPIC: ([CPG], ["goal_manager"], 1)})
    r = rb.send_pddl_goal("euclid", "(visited-object o1)", engine="grstaps")
    assert r["via"] == "goal_manager" and r["engine"] == "grstaps"
    assert sent[-1][:2] == ("/willow/commanded_goal", COMMANDED_GOAL_TYPE)


def test_send_grstaps_straight_to_the_discovered_topic(monkeypatch):
    # GRSTAPS-X plugin up, no goal_manager: straight there, though not in our config
    rb, sent = _bridge(monkeypatch, {GR_TOPIC: ([CPG], [], 1)}, gm_subs=0)
    r = rb.send_pddl_goal("euclid", "(visited-object o1)", engine="grstaps")
    assert r["via"] == "planner" and r["plugin"] == "grstaps_planner"
    assert sent[-1][0] == GR_TOPIC and sent[-1][1] == CPG
    assert sent[-1][2]["goal"] == {
        "robot_id": "euclid",
        "pddl_goal": "(visited-object o1)",
    }


def test_send_grstaps_without_any_planner_still_refuses(monkeypatch):
    rb, sent = _bridge(monkeypatch, {}, gm_subs=0)
    with pytest.raises(LookupError, match="no GRSTAPS-X planner"):
        rb.send_pddl_goal("euclid", "(visited-object o1)", engine="grstaps")
    assert sent == []


def test_chat_toggle_offers_grstaps_from_the_live_graph(monkeypatch):
    rb, _ = _bridge(monkeypatch, {})
    rb.available = True
    c = _chat(rb)
    assert [e["id"] for e in c.chat_engines()] == ["fast_downward"]  # not offered
    # the grstaps_repair session starts after the console
    rb.node.graph[GR_TOPIC] = ([CPG], ["goal_manager"], 1)
    rb._plugin_graph.clear()  # (the cache would expire within 3 s)
    gr = {e["id"]: e for e in c.chat_engines()}["grstaps"]
    assert gr["available"] and gr["reason"] is None


# ------------------------------------------------- engine routing matrix
FD_TOPIC = "/willow/omniplanner_node/multi_robot_pddl_constrained/pddl_goal"
GOAL = "(visited-object o1)"
CONS = [["forbidden-poi", "o14"]]


def _session(monkeypatch, gm_engine, fd_loaded=True, gr_loaded=True):
    """A base session whose goal_manager forwards to ``gm_engine``."""
    graph = {}
    if gr_loaded:
        graph[GR_TOPIC] = ([CPG], ["goal_manager"] if gm_engine == "grstaps" else [], 1)
    if fd_loaded:
        graph[FD_TOPIC] = (
            [CPG],
            ["goal_manager"] if gm_engine == "fast_downward" else [],
            1,
        )
    rb, sent = _bridge(monkeypatch, graph, gm_subs=1)
    rb.plugins = []  # nothing from the console's own config: the live graph decides
    return rb, sent


def test_fd_goal_in_a_grstaps_session_goes_to_fast_downward_directly(monkeypatch):
    rb, sent = _session(monkeypatch, "grstaps")
    assert rb.goal_manager_engine() == "grstaps"
    r = rb.send_pddl_goal("euclid", GOAL, constraints=CONS, engine="fast_downward")
    topic, msg_type, fields = sent[-1]
    assert topic == FD_TOPIC and msg_type == CPG
    assert fields == {"goal": {"robot_id": "euclid", "pddl_goal": GOAL},
                      "constraints": [{"predicate": "forbidden-poi", "symbols": ["o14"]}]}  # fmt: skip
    assert r["via"] == "planner" and r["engine"] == "fast_downward"
    assert r["plugin"] == "multi_robot_pddl_constrained"
    assert "sent to Fast Downward directly" in r["warning"]
    assert (
        "runs GRSTAPS-X in this session" in r["warning"]
        and "not repair-tracked" in r["warning"]
    )


def test_fd_goal_in_a_grstaps_session_without_fd_loaded_is_refused(monkeypatch):
    rb, sent = _session(monkeypatch, "grstaps", fd_loaded=False)
    with pytest.raises(LookupError, match="Fast Downward is not loaded"):
        rb.send_pddl_goal("euclid", GOAL, engine="fast_downward")
    assert sent == []


def test_fd_goal_forced_through_a_grstaps_goal_manager_is_refused(monkeypatch):
    rb, sent = _session(monkeypatch, "grstaps")
    with pytest.raises(LookupError, match="goal_manager runs GRSTAPS-X"):
        rb.send_pddl_goal("euclid", GOAL, engine="fast_downward", route="goal_manager")
    assert sent == []


def test_no_engine_asked_keeps_the_sessions_plan_repair(monkeypatch):
    rb, sent = _session(monkeypatch, "grstaps")
    r = rb.send_pddl_goal("euclid", GOAL)
    assert r["via"] == "goal_manager" and sent[-1][0] == "/willow/commanded_goal"


@pytest.mark.parametrize("engine", ["fast_downward", None])
def test_fd_goal_in_an_fd_session_goes_through_goal_manager(monkeypatch, engine):
    rb, sent = _session(monkeypatch, "fast_downward", gr_loaded=False)
    r = rb.send_pddl_goal("euclid", GOAL, engine=engine)
    assert r["via"] == "goal_manager" and sent[-1][:2] == (
        "/willow/commanded_goal",
        COMMANDED_GOAL_TYPE,
    )


def test_grstaps_goal_in_a_grstaps_session_goes_through_goal_manager(monkeypatch):
    rb, sent = _session(monkeypatch, "grstaps")
    r = rb.send_pddl_goal("euclid", GOAL, engine="grstaps")
    assert r["via"] == "goal_manager" and r["engine"] == "grstaps"


def test_grstaps_goal_in_an_fd_session_goes_to_grstaps_directly(monkeypatch):
    rb, sent = _session(monkeypatch, "fast_downward")
    r = rb.send_pddl_goal("euclid", GOAL, engine="grstaps")
    assert r["via"] == "planner" and sent[-1][0] == GR_TOPIC
    assert "sent to GRSTAPS-X directly" in r["warning"]
    assert "runs Fast Downward in this session" in r["warning"]


def test_grstaps_goal_in_an_fd_session_without_grstaps_is_refused(monkeypatch):
    rb, sent = _session(monkeypatch, "fast_downward", gr_loaded=False)
    with pytest.raises(LookupError, match="no GRSTAPS-X planner"):
        rb.send_pddl_goal("euclid", GOAL, engine="grstaps")
    with pytest.raises(LookupError, match="forwards to Fast Downward"):
        rb.send_pddl_goal("euclid", GOAL, engine="grstaps", route="goal_manager")
    assert sent == []


def test_chat_toggle_marks_the_sessions_plan_repair_engine(monkeypatch):
    rb, _ = _session(monkeypatch, "grstaps")
    rb.available = True
    c = _chat(rb)
    eng = {e["id"]: e for e in c.chat_engines()}
    assert eng["grstaps"]["repair"] is True and eng["fast_downward"]["repair"] is False
