"""The Planner state view: what the planner believes (holding, plan robots and
states, constraints, versions, last error), and correcting holding through
heracles_state_updater's update_holding_state."""

import json
import types

import pytest

from adt4_console import planner_state as ps_mod
from adt4_console.planner_state import HoldingError, PlannerState, neo4j_symbol
from adt4_console.ros_bridge import RosBridge

from .conftest import NEO4J_AUTH, NEO4J_URI, make_client, port_open


def _bridge():
    return RosBridge(types.SimpleNamespace(ros=False, planner_ns="willow"))


def _msg(data):
    return types.SimpleNamespace(data=data)


class _Src:
    """A Neo4j source answering the planner state's Cypher from dicts."""

    id = "neo4j:test"

    def __init__(self, holding=None, objects=None, poses=None, version=7, epoch="e1"):
        self.holding = {r: list(o) for r, o in (holding or {}).items()}
        self.objects = dict(objects or {})  # symbol -> (x, y, z)
        self.poses = dict(poses or {})
        self.version, self.epoch = version, epoch

    def query(self, cypher, **kw):
        if "[:HOLDS]" in cypher:
            return [
                {"robot": r, "object": o}
                for r, objs in sorted(self.holding.items())
                for o in objs
            ]
        if "r.position AS p" in cypher:
            pt = lambda p: types.SimpleNamespace(x=p[0], y=p[1], z=p[2])  # noqa: E731
            return [{"n": n, "p": pt(p)} for n, p in self.poses.items()]
        if "_MapState" in cypher:
            return [{"v": self.version, "e": self.epoch, "t": None, "f": None}]
        if "o.center AS c" in cypher:
            c = self.objects.get(kw["s"])
            if c is None:
                return []
            return [{"c": types.SimpleNamespace(x=c[0], y=c[1], z=c[2]), "k": "box"}]
        if "o.nodeSymbol AS s" in cypher:
            return [{"s": kw["s"]}] if kw["s"] in self.objects else []
        raise AssertionError(cypher)


class _Ros:
    """The bridge as PlannerState uses it; holding calls change the fake map."""

    available = True

    def __init__(self, src, fail=None, view=None, errors=None):
        self.src, self.fail = src, fail
        self.calls = []
        self.view = view or {}
        self.errors = errors or {}

    def planner_view(self):
        return dict(self.view)

    def last_plan_errors(self):
        return dict(self.errors)

    def constraints(self, robot):
        return (
            [{"predicate": "forbidden-poi", "symbols": ["o3"]}]
            if robot == "hamilton"
            else []
        )

    def update_holding_state(self, robot, obj, is_holding, position=None, timeout=10.0):
        self.calls.append((robot, obj, is_holding, position))
        if self.fail == (robot, obj):
            return {"success": False}
        held = self.src.holding.setdefault(robot, [])
        if is_holding:
            held.append(obj)
        else:
            held.remove(obj)
            if position is not None:
                self.src.objects[obj] = tuple(position)
        return {"success": True}


def _ps(src, ros, robots=("euclid", "hamilton")):
    hub = types.SimpleNamespace(sent=[], publish=lambda t, d: hub.sent.append((t, d)))
    maps = types.SimpleNamespace(target=lambda: src)
    return PlannerState(
        types.SimpleNamespace(robots=list(robots)), ros, maps, None, hub
    ), hub


# ------------------------------------------------------------------ ros bridge
def test_bridge_keeps_goal_manager_states_and_plan_robots():
    rb = _bridge()
    states = {"euclid": {"status": "running", "plan_id": "p1", "done": 1, "total": 4,
                         "conjuncts": ["(holding euclid o22)"], "remaining": []}}  # fmt: skip
    rb._on_robot_plan_states(_msg(json.dumps(states)))
    rb._on_plan_robots(_msg(json.dumps(["euclid"])))
    rb._on_planner_map_version(types.SimpleNamespace(data=12))
    rb._planner_set(map_epoch="abc")
    v = rb.planner_view()
    assert v["robot_plan_states"] == states and v["plan_robots"] == ["euclid"]
    assert v["map_version"] == 12 and v["map_epoch"] == "abc"
    assert v["robot_plan_states_t"] > 0
    rb._on_robot_plan_states(_msg("not json"))  # ignored
    assert rb.planner_view()["robot_plan_states"] == states
    assert rb.robot_plan_states_topic() == "/willow/goal_manager/robot_plan_states"


def test_bridge_last_plan_error_is_the_newest_failure():
    rb = _bridge()
    rb._last_goal_robot = "euclid"
    rb._on_plan_status("euclid", _msg(json.dumps({"plugin": "grstaps", "robot": "euclid", "ok": False,
                                                  "error": "no plan", "t": 10.0})))  # fmt: skip
    rb._on_plan_status(
        "euclid",
        _msg(
            json.dumps({"plugin": "grstaps", "robot": "euclid", "ok": True, "t": 20.0})
        ),
    )
    assert rb.last_plan_errors()["euclid"]["text"] == "no plan"
    rb._on_repair_alert("euclid", "plan_failed", "goal unreachable")
    err = rb.last_plan_errors()["euclid"]
    assert err["text"] == "goal unreachable" and err["source"] == "plan_failed"
    assert "hamilton" not in rb.last_plan_errors()


def test_bridge_tells_the_view_when_something_changes():
    rb = _bridge()
    told = []
    rb.planner_state = types.SimpleNamespace(notify=lambda: told.append(1))
    rb._on_plan_robots(_msg('["euclid"]'))
    rb.add_constraints("euclid", [["forbidden-poi", "o1"]])
    rb.clear_constraints("euclid")
    rb._on_plan_status(
        "euclid", _msg('{"robot": "euclid", "ok": false, "error": "x", "t": 1}')
    )
    assert len(told) == 4


def test_bridge_update_holding_state_calls_the_state_updater(monkeypatch):
    pytest.importorskip("geometry_msgs.msg")
    rb = _bridge()
    calls = []
    monkeypatch.setattr(
        rb,
        "call_service",
        lambda name, t, fields, timeout=10.0: calls.append((name, t, fields))
        or {"success": True},
    )
    assert rb.update_holding_state("euclid", "O11", False, (1.0, 2.0, 0.5))["success"]
    name, srv, fields = calls[-1]
    assert name == "/euclid/update_holding_state"
    assert srv == "heracles_ros_interfaces/srv/UpdateHoldingState"
    assert (
        fields["id"] == "O11"
        and fields["is_holding"] is False
        and fields["has_position"]
    )
    assert (fields["position"].x, fields["position"].y, fields["position"].z) == (
        1.0,
        2.0,
        0.5,
    )
    rb.update_holding_state("euclid", "O22", True)
    assert calls[-1][2] == {"id": "O22", "is_holding": True}


# --------------------------------------------------------------- the snapshot
def test_snapshot_puts_together_what_the_planner_plans_from():
    src = _Src(
        holding={"euclid": ["O11"]},
        poses={"euclid": (1.0, 2.0, 0.0)},
        version=9,
        epoch="ep",
    )
    ros = _Ros(src, view={"plan_robots": ["euclid"], "map_version": 4, "map_epoch": "ep",
                          "robot_plan_states": {"euclid": {"status": "running", "done": 0, "total": 3}}},
               errors={"hamilton": {"t": 5.0, "text": "no plan", "robot": "hamilton"}})  # fmt: skip
    ps, _ = _ps(src, ros)
    snap = ps.snapshot()
    by = {r["robot"]: r for r in snap["robots"]}
    assert by["euclid"]["holding"] == ["O11"] and by["euclid"]["position"] == [
        1.0,
        2.0,
        0.0,
    ]
    assert by["euclid"]["in_plan"] is True and by["hamilton"]["in_plan"] is False
    assert by["euclid"]["plan_state"]["status"] == "running"
    assert by["hamilton"]["constraints"] == [
        {"predicate": "forbidden-poi", "symbols": ["o3"]}
    ]
    assert by["hamilton"]["last_error"]["text"] == "no plan"
    assert snap["last_error"]["robot"] == "hamilton"
    assert (
        snap["map"]["version"] == 9
        and snap["map"]["epoch"] == "ep"
        and snap["map"]["error"] is None
    )
    assert (
        snap["planner"]["map_version"] == 4 and snap["planner"]["goal_manager"] is True
    )


def test_snapshot_lists_a_robot_outside_the_roster_that_holds_something():
    src = _Src(holding={"gauss": ["O2"]})
    ps, _ = _ps(src, _Ros(src))
    robots = [r["robot"] for r in ps.snapshot()["robots"]]
    assert robots == ["euclid", "hamilton", "gauss"]


def test_snapshot_without_a_map_says_why():
    ros = _Ros(None)
    maps = types.SimpleNamespace(
        target=lambda: (_ for _ in ()).throw(LookupError("no Neo4j target"))
    )
    ps = PlannerState(types.SimpleNamespace(robots=["euclid"]), ros, maps)
    snap = ps.snapshot()
    assert "no Neo4j target" in snap["map"]["error"]
    assert snap["robots"][0]["holding"] == [] and snap["planner"]["plan_robots"] is None


# ------------------------------------------------------------ holding changes
def test_clearing_releases_each_object_where_the_map_has_it():
    src = _Src(holding={"euclid": ["O11"]}, objects={"O11": (3.0, 4.0, 0.2)})
    ros = _Ros(src)
    ps, hub = _ps(src, ros)
    out = ps.set_holding("euclid", None, author="harel")
    assert out["ok"] and out["holding"] == {}
    assert ros.calls == [("euclid", "O11", False, (3.0, 4.0, 0.2))]
    rec = ps.snapshot()["corrections"][-1]
    assert rec["request"] == "euclid holds nothing" and rec["before"] == {
        "euclid": ["O11"]
    }
    assert rec["author"] == "harel" and rec["after"] == {}
    assert hub.sent and hub.sent[-1][0] == ps_mod.CHANGED_TOPIC


def test_setting_holds_x_releases_the_rest_and_takes_x_from_another_robot():
    src = _Src(holding={"euclid": ["O11"], "hamilton": ["O22"]},
               objects={"O11": (0, 0, 0), "O22": (1, 1, 0)})  # fmt: skip
    ros = _Ros(src)
    ps, _ = _ps(src, ros)
    out = ps.set_holding("euclid", "o22")
    assert out["ok"] and out["holding"] == {"euclid": ["O22"]}
    assert [(r, o, h) for r, o, h, _ in ros.calls] == [
        ("euclid", "O11", False), ("hamilton", "O22", False), ("euclid", "O22", True)]  # fmt: skip


def test_a_correction_that_changes_nothing_calls_nothing():
    src = _Src(holding={"euclid": ["O22"]}, objects={"O22": (0, 0, 0)})
    ros = _Ros(src)
    ps, _ = _ps(src, ros)
    out = ps.set_holding("euclid", "O(22)")
    assert out["ok"] and out["noop"] and ros.calls == []


def test_an_unknown_object_is_refused():
    src = _Src(objects={"O1": (0, 0, 0)})
    ps, _ = _ps(src, _Ros(src))
    with pytest.raises(HoldingError):
        ps.set_holding("euclid", "O99")
    with pytest.raises(ValueError):
        ps.set_holding("euclid", "not a symbol")


def test_a_failed_step_stops_and_is_reported():
    src = _Src(
        holding={"euclid": ["O11", "O12"]}, objects={"O11": (0, 0, 0), "O12": (0, 0, 0)}
    )
    ros = _Ros(src, fail=("euclid", "O11"))
    ps, _ = _ps(src, ros)
    out = ps.set_holding("euclid", None)
    assert (
        not out["ok"]
        and len(out["steps"]) == 1
        and out["steps"][0]["service"] == "/euclid/update_holding_state"
    )
    assert not ps.snapshot()["corrections"][-1]["ok"]


def test_neo4j_symbol():
    assert neo4j_symbol("O(11)") == neo4j_symbol("o11") == neo4j_symbol("O11") == "O11"


# ---------------------------------------------------------------------- routes
def test_routes(settings, planner_plugins):
    client, svc = make_client(settings)
    with client:
        r = client.get("/api/plan/planner_state")
        assert r.status_code == 200, r.text
        snap = r.json()
        assert snap["ros"] is False and "robots" in snap and snap["map"]["error"]
        r = client.post(
            "/api/plan/planner_state/holding", json={"robot": "euclid", "object": None}
        )
        assert r.status_code == 400 and "confirm" in r.json()["detail"]
        r = client.post(
            "/api/plan/planner_state/holding", json={"robot": "euclid", "confirm": True}
        )
        assert r.status_code == 503  # ROS disabled in tests


def test_route_sets_holding_through_the_service(settings, planner_plugins, monkeypatch):
    client, svc = make_client(settings)
    src = _Src(
        holding={"euclid": ["O11"]}, objects={"O11": (1, 2, 3), "O22": (0, 0, 0)}
    )
    ros = _Ros(src)
    svc.planner_state.maps = types.SimpleNamespace(target=lambda: src)
    monkeypatch.setattr(svc.ros, "available", True)
    monkeypatch.setattr(svc.ros, "update_holding_state", ros.update_holding_state)
    with client:
        r = client.post("/api/plan/planner_state/holding",
                        json={"robot": "euclid", "object": "O22", "confirm": True},
                        headers={"x-operator": "harel"})  # fmt: skip
        assert r.status_code == 200, r.text
        assert r.json()["holding"] == {"euclid": ["O22"]}
        r = client.post(
            "/api/plan/planner_state/holding",
            json={"robot": "euclid", "object": "O77", "confirm": True},
        )
        assert r.status_code == 400 and "not an object" in r.json()["detail"]
        ros.fail = ("euclid", "O22")
        r = client.post(
            "/api/plan/planner_state/holding", json={"robot": "euclid", "confirm": True}
        )
        assert (
            r.status_code == 502
            and "/euclid/update_holding_state" in r.json()["detail"]
        )
        assert (
            client.get("/api/plan/planner_state").json()["corrections"][0]["author"]
            == "harel"
        )


# -------------------------------------------------------- Neo4j (:7697 only)
@pytest.fixture
def neo(settings, planner_plugins):
    host, port = NEO4J_URI.split("//")[-1].split(":")
    if port != "7697":
        pytest.skip("holding tests write only to the throwaway Neo4j on :7697")
    if not port_open(host, int(port)):
        pytest.skip(f"no Neo4j at {NEO4J_URI}")
    settings.neo4j = {"ps": NEO4J_URI}
    settings.neo4j_user, settings.neo4j_password = NEO4J_AUTH
    client, svc = make_client(settings)
    src = svc.graph.get("neo4j:ps")
    try:
        src.db()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Neo4j unusable: {e}")
    q = src.query
    q(
        "MERGE (r:Robot {name: 'ps_test_bot'}) SET r.position = point({x: 1.5, y: -2.0, z: 0.0})"
    )
    q(
        "MERGE (o:Object {nodeSymbol: 'O990001'}) SET o.center = point({x: 4.0, y: 5.0, z: 0.5}), o.class = 'ps test box'"
    )
    q(
        "MERGE (o:Object {nodeSymbol: 'O990002'}) SET o.center = point({x: 6.0, y: 5.0, z: 0.5}), o.class = 'ps test cone'"
    )
    q(
        "MATCH (r:Robot {name: 'ps_test_bot'}), (o:Object {nodeSymbol: 'O990001'}) MERGE (r)-[:HOLDS]->(o)"
    )
    with client:
        yield client, svc, q
    q("MATCH (r:Robot {name: 'ps_test_bot'}) DETACH DELETE r")
    q("MATCH (o:Object) WHERE o.nodeSymbol IN ['O990001', 'O990002'] DETACH DELETE o")


def test_holding_comes_from_the_map_and_is_corrected_there(neo, monkeypatch):
    client, svc, q = neo

    def update(robot, obj, is_holding, position=None, timeout=10.0):
        """What heracles_state_updater does with the call (HOLDS + object center)."""
        if is_holding:
            q(
                "MATCH (r:Robot {name: $r}), (o:Object {nodeSymbol: $o}) MERGE (r)-[:HOLDS]->(o)",
                r=robot,
                o=obj,
            )
        else:
            q(
                "MATCH (:Robot {name: $r})-[h:HOLDS]->(:Object {nodeSymbol: $o}) DELETE h",
                r=robot,
                o=obj,
            )
            if position is not None:
                q("MATCH (o:Object {nodeSymbol: $o}) SET o.center = point({x: $x, y: $y, z: $z})",
                  o=obj, x=position[0], y=position[1], z=position[2])  # fmt: skip
        return {"success": True}

    monkeypatch.setattr(svc.ros, "available", True)
    monkeypatch.setattr(svc.ros, "update_holding_state", update)
    snap = client.get("/api/plan/planner_state").json()
    bot = next(r for r in snap["robots"] if r["robot"] == "ps_test_bot")
    assert bot["holding"] == ["O990001"] and bot["position"] == [1.5, -2.0, 0.0]
    assert snap["map"]["source"] == "neo4j:ps" and snap["map"]["error"] is None

    r = client.post(
        "/api/plan/planner_state/holding",
        json={"robot": "ps_test_bot", "object": "o990002", "confirm": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["holding"].get("ps_test_bot") == ["O990002"]
    # the released object stays where the map had it
    c = q("MATCH (o:Object {nodeSymbol: 'O990001'}) RETURN o.center AS c")[0]["c"]
    assert (c.x, c.y) == (4.0, 5.0)

    r = client.post(
        "/api/plan/planner_state/holding",
        json={"robot": "ps_test_bot", "object": None, "confirm": True},
    )
    assert r.status_code == 200 and "ps_test_bot" not in r.json()["holding"]
