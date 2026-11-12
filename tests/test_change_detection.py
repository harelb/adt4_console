"""Change detection start / stop / reset (API.md §19): the relocalization gate."""

from adt4_console.routes.change_detection import hydra_matches, relocalized

from .conftest import make_client

FA_OK = {
    "parent": "map",
    "child": "euclid/odom",
    "ok": True,
    "T": [0, 0, 0, 1, 0, 0, 0],
}


def _hydra(count, nickname="euclid/hydra_multi", status="nominal"):
    """A C1 ``health`` table with hydra_multi's monitor row."""
    return {
        "nodes": [
            {"nickname": "euclid/roman_lc", "status": "nominal", "notes": "ok"},
            {
                "nickname": nickname,
                "node_name": "hydra_multi",
                "status": status,
                "notes": f"{count} valid interrobot loop closures",
            },
        ]
    }


def test_relocalized_verdicts():
    assert not relocalized(None)["ok"]
    assert "older agent" in relocalized({"pose": {}})["reason"]
    assert not relocalized({"frame_align": {**FA_OK, "ok": False}})["ok"]
    assert not relocalized({"frame_align": {**FA_OK, "child": "map"}})["ok"]
    assert relocalized({"frame_align": FA_OK})[
        "ok"
    ]  # no ROMAN info: the transform decides
    no_match = relocalized(
        {"frame_align": FA_OK, "reloc": {"prior_lcs": 0}, "health": _hydra("0 / 3")}
    )
    assert not no_match["ok"] and "no prior-map match" in no_match["reason"]
    ok = relocalized(
        {
            "frame_align": FA_OK,
            "reloc": {"prior_lcs": 3, "recent_jumps": [{"dxy": 4.0, "dyaw": 1}]},
        }
    )
    assert ok["ok"] and "3 prior-map matches" in ok["reason"]
    sus = relocalized(
        {
            "frame_align": FA_OK,
            "reloc": {
                "prior_lcs": 3,
                "recent_jumps": [{"dxy": 4, "dyaw": 0}, {"dxy": 0.1, "dyaw": 45}],
            },
        }
    )
    assert not sus["ok"] and "suspect" in sus["reason"]


class FakeRos:
    available = True
    error = None

    def __init__(self):
        self.calls = []

    def ensure_robot(self, robot):
        pass

    def change_detection(self, robot):
        return {"robot": robot, "state": "paused", "t": 1.0}

    def change_detection_cmd(self, robot, cmd):
        self.calls.append((robot, cmd))
        return {
            "ok": True,
            "cmd": cmd,
            "message": {"start": "started", "reset": "reset requested"}.get(cmd, cmd),
        }

    def clear_change_detection(self, robots):
        return {"ok": True, "robots": robots}


def test_routes_gate_start_until_relocalized(settings):
    client, svc = make_client(settings)
    ros = FakeRos()
    states = {"euclid": {"frame_align": FA_OK, "reloc": {"prior_lcs": 0}}}
    with client:
        svc.ros = ros  # after startup (the real bridge started with ROS off)
        svc.live.state = lambda r: states.get(r)
        r = client.get("/api/robots/euclid/change_detection").json()
        assert r["state"] == "paused" and not r["relocalized"]["ok"]
        r = client.post("/api/robots/euclid/change_detection/start", json={})
        assert r.status_code == 409 and r.json()["not_relocalized"] and ros.calls == []
        # stop / reset are never gated
        assert client.post("/api/robots/euclid/change_detection/stop").json()["ok"]
        r = client.post(
            "/api/robots/euclid/change_detection/reset_start", json={"override": True}
        ).json()
        assert (
            r["ok"]
            and r["override"]
            and ros.calls[-2:] == [("euclid", "reset"), ("euclid", "start")]
        )
        states["euclid"]["reloc"]["prior_lcs"] = 2
        r = client.post("/api/robots/euclid/change_detection/start", json={}).json()
        assert r["ok"] and not r["override"] and ros.calls[-1] == ("euclid", "start")
        assert (
            client.post("/api/robots/euclid/change_detection/bogus").status_code == 404
        )
        assert client.post(
            "/api/change_detection/clear", json={"robots": ["euclid"]}
        ).json()["robots"] == ["euclid"]


def test_ros_off_is_503(settings):
    client, svc = make_client(settings)
    with client:
        assert (
            client.post("/api/robots/euclid/change_detection/stop").status_code == 503
        )


# ------------------------------------------------- AWCD change set (overlay)
from types import SimpleNamespace as NS  # noqa: E402

from adt4_console.awcd_changes import (  # noqa: E402
    AwcdChangeSet,
    awcd_msg_to_dict,
    namespace_crc,
)


def _info(oid, name, c, dims=(1.0, 1.0, 1.0)):
    v = lambda x, y, z: NS(x=x, y=y, z=z)  # noqa: E731
    return NS(
        id=oid,
        name=name,
        bbox_center=v(*c),
        bbox_dimensions=v(*dims),
        bbox_orientation=NS(x=0.0, y=0.0, z=0.0, w=1.0),
        stamp=NS(sec=10, nanosec=500_000_000),
        change_confidence=0.9,
        confidence=0.8,
        num_frames_observed=7,
    )


def _msg(robot, added=(), removed=(), frame="map"):
    return NS(
        robot_name=robot,
        header=NS(frame_id=frame, stamp=NS(sec=12, nanosec=0)),
        added_objects=list(added),
        removed_objects=list(removed),
    )


def test_awcd_msg_to_dict():
    d = awcd_msg_to_dict(_msg("hamilton", added=[_info(3, "chair", (1, 2, 0.5))]))
    assert d["robot"] == "hamilton" and d["frame_id"] == "map" and d["removed"] == []
    o = d["added"][0]
    assert o["id"] == 3 and o["label"] == "chair" and o["center"] == [1, 2, 0.5]
    assert o["quat"] == [0, 0, 0, 1] and o["stamp"] == 10.5 and o["frames"] == 7


def test_awcd_change_set_attribution_and_clear():
    cs = AwcdChangeSet(clock=lambda: 100.0)
    assert cs.state()["changes"] == [] and cs.state()["topic"] is None
    cs.on_robot(awcd_msg_to_dict(_msg("hamilton", added=[_info(5, "bag", (0, 0, 0))],
                                      removed=[_info(42, "cone", (3, 3, 0))])))  # fmt: skip
    cs.on_robot(
        awcd_msg_to_dict(_msg("euclid", removed=[_info(42, "cone", (3, 3, 0))]))
    )
    cs.on_robot(awcd_msg_to_dict(_msg("reconciled")))  # ignored
    added_id = (namespace_crc("hamilton") << 32) | 5
    rec = _msg("reconciled", added=[_info(added_id, "bag", (0, 0, 0))],
               removed=[_info(42, "cone", (3, 3, 0))])  # fmt: skip
    cs.on_reconciled(
        awcd_msg_to_dict(rec), "/willow/awcd_base_station/reconciled_changes"
    )
    st = cs.state(["euclid", "hamilton", "gauss"])
    assert st["topic"].endswith("reconciled_changes") and st["t"] == 100.0
    assert st["counts"] == {"added": 1, "removed": 1}
    removed, added = st["changes"]
    assert removed["kind"] == "removed" and removed["robots"] == ["euclid", "hamilton"]
    assert added["kind"] == "added" and added["robots"] == ["hamilton"]
    assert (
        added["key"] == f"a{added_id}"
        and added["label"] == "bag"
        and added["short_id"] == 5
    )
    assert set(st["robots"]) == {"euclid", "hamilton"}
    assert st["robots"]["hamilton"] == {"t": 100.0, "added": 1, "removed": 1}
    # a base station clear: robots' reports dropped; the reconciled set follows
    # when the base station republishes it (an empty set here)
    cs.forget(["euclid"])
    assert cs.state()["changes"][0]["robots"] == ["hamilton"]
    cs.on_reconciled(awcd_msg_to_dict(_msg("reconciled")))
    assert cs.state()["changes"] == [] and cs.state()["topic"].endswith(
        "reconciled_changes"
    )
    cs.forget()
    assert cs.state()["robots"] == {}


def test_changes_route(settings):
    client, svc = make_client(settings)
    with client:
        # ROS off: an empty, unavailable set (the UI hides the overlay)
        r = client.get("/api/change_detection/changes").json()
        assert r["available"] is False and r["changes"] == []
        cs = AwcdChangeSet()
        cs.on_reconciled(
            awcd_msg_to_dict(_msg("reconciled", removed=[_info(1, "cone", (0, 0, 0))]))
        )
        ros = FakeRos()
        ros.awcd_changes = lambda: cs.state()
        svc.ros = ros
        r = client.get("/api/change_detection/changes").json()
        assert r["available"] and r["changes"][0]["kind"] == "removed"


def test_bridge_routes_awcd_messages():
    """RosBridge._on_awcd: reconciled vs per-robot, hub push; clear forgets."""
    from adt4_console.ros_bridge import RosBridge

    pushed = []
    hub = NS(publish=lambda topic, data: pushed.append((topic, data)))
    br = RosBridge(NS(ros=False, planner_ns="willow", ros_domain_id=0), hub=hub)
    br._on_awcd(_msg("hamilton", removed=[_info(9, "cone", (0, 0, 0))]), None)
    br._on_awcd(_msg("reconciled", removed=[_info(9, "cone", (0, 0, 0))]),
                "/willow/awcd_base_station/reconciled_changes")  # fmt: skip
    assert [t for t, _ in pushed] == ["awcd/changes", "awcd/changes"]
    last = pushed[-1][1]
    assert (
        last["changes"][0]["robots"] == ["hamilton"] and last["counts"]["removed"] == 1
    )
    calls = []
    br.run = lambda fn, timeout=5.0: [("/willow/awcd_base_station/clear_robots", ["x"])]
    br.call_service = lambda name, typ, req, timeout=0: calls.append((name, req)) or {
        "success": True
    }
    out = br.clear_change_detection(["hamilton"])
    assert out["ok"] and calls == [
        ("/willow/awcd_base_station/clear_robots", {"robot_names": ["hamilton"]})
    ]
    assert br.awcd_changes()["robots"] == {}


# 2026-11-11 robot test: hydra_multi accepted 44 / 51 (hamilton) and 14 / 19
# (euclid) prior-map loop closures while fleetlink's ROMAN counter stayed 0.
RELOC_ZERO = {"prior_lcs": 0, "self_lcs": 0, "jumps": 5, "total": {"dxy": 0.0, "dyaw": 0.0}}  # fmt: skip
FA_MOVED = {**FA_OK, "child": "hamilton/odom", "T": [-25.0, 7.5, 0, 1, 0, 0, 0]}


def test_hydra_multi_count_is_the_best_evidence():
    r = relocalized(
        {"frame_align": FA_OK, "reloc": RELOC_ZERO, "health": _hydra("44 / 51")},
        "hamilton",
    )
    assert r["ok"] and r["state"] == "relocalized" and r["source"] == "hydra_multi"
    assert r["matches"] == 44 and "44 accepted prior-map matches" in r["reason"]
    assert "not relocalized" not in r["reason"]
    # bare nickname, and no reloc at all (older agent) still count
    r = relocalized(
        {"frame_align": FA_OK, "health": _hydra("14 / 19", nickname="hydra_multi")}
    )
    assert r["ok"] and r["matches"] == 14
    # even before the agent sends a usable transform
    r = relocalized({"frame_align": {**FA_OK, "ok": False}, "health": _hydra("2 / 2")})
    assert r["ok"] and r["source"] == "hydra_multi"
    # a positive count is still suspect after two big jumps
    jumpy = {
        **RELOC_ZERO,
        "recent_jumps": [{"dxy": 4, "dyaw": 0}, {"dxy": 9, "dyaw": 0}],
    }
    r = relocalized({"frame_align": FA_OK, "reloc": jumpy, "health": _hydra("44 / 51")})
    assert not r["ok"] and r["state"] == "suspect"


def test_hydra_matches_parses_only_the_count_note():
    assert hydra_matches(_hydra("44 / 51")) == (44, 51)
    assert hydra_matches(_hydra("0 / 0")) == (0, 0)
    stale = _hydra("x", status="error")
    stale["nodes"][1]["notes"] = "time since last spin: 1.45e+03 [s]"
    assert hydra_matches(stale) is None
    waiting = _hydra("x", status="warning")
    waiting["nodes"][1]["notes"] = "waiting for scene graphs"
    assert hydra_matches(waiting) is None
    assert hydra_matches({}) is None and hydra_matches(None) is None
    # the count of another node's note is not hydra_multi's
    other = {"nodes": [{"nickname": "x/roman_lc", "notes": "3 / 4 valid interrobot loop closures"}]}  # fmt: skip
    assert hydra_matches(other) is None


def test_roman_counter_is_second():
    r = relocalized(
        {"frame_align": FA_OK, "reloc": {**RELOC_ZERO, "prior_lcs": 3}},
        "hamilton",
    )
    assert r["ok"] and r["source"] == "roman" and r["matches"] == 3


def test_map_odom_move_is_a_weaker_aligned_signal():
    # from identity (agent started after the relocalization: no move of its own)
    r = relocalized({"frame_align": FA_MOVED, "reloc": RELOC_ZERO}, "hamilton")
    assert r["ok"] and r["state"] == "aligned" and r["source"] == "map_odom"
    assert "no match count" in r["reason"] and "26.1 m" in r["reason"]
    # since the agent's first sample
    moved = {**RELOC_ZERO, "total": {"dxy": 0.1, "dyaw": 12.0}}
    r = relocalized({"frame_align": FA_OK, "reloc": moved})
    assert r["ok"] and r["state"] == "aligned" and "12°" in r["reason"]
    # optimizer noise is not a move
    noise = {**FA_OK, "T": [0.2, 0.1, 0, 1, 0, 0, 0]}
    assert (
        relocalized({"frame_align": noise, "reloc": RELOC_ZERO})["state"] == "unknown"
    )


def test_no_signal_is_unknown_not_not_relocalized():
    # euclid: health table reached the base empty, ROMAN counter 0, near identity
    r = relocalized(
        {"frame_align": FA_OK, "reloc": RELOC_ZERO, "health": {"nodes": []}}, "euclid"
    )
    assert not r["ok"] and r["state"] == "unknown"
    assert r["reason"].startswith("unknown (no relocalization signal from euclid")
    assert "not relocalized" not in r["reason"]
    assert not relocalized(None, "euclid")["ok"]
    # only a reported zero count says "not relocalized ... initial estimate"
    r = relocalized(
        {"frame_align": FA_OK, "reloc": RELOC_ZERO, "health": _hydra("0 / 5")}, "euclid"
    )
    assert not r["ok"] and r["state"] == "not_relocalized"
    assert "initial estimate" in r["reason"]


def test_routes_gate_start_on_hydra_multi_count(settings):
    client, svc = make_client(settings)
    ros = FakeRos()
    states = {"hamilton": {"frame_align": FA_OK, "reloc": dict(RELOC_ZERO), "health": {}}}  # fmt: skip
    with client:
        svc.ros = ros
        svc.live.state = lambda r: states.get(r)
        r = client.get("/api/robots/hamilton/change_detection").json()
        assert r["relocalized"]["state"] == "unknown"
        r = client.post("/api/robots/hamilton/change_detection/start", json={})
        assert r.status_code == 409 and "no relocalization signal from hamilton" in r.json()["reason"]  # fmt: skip
        states["hamilton"]["health"] = _hydra(
            "44 / 51", nickname="hamilton/hydra_multi"
        )
        r = client.post("/api/robots/hamilton/change_detection/start", json={}).json()
        assert r["ok"] and not r["override"] and r["relocalized"]["matches"] == 44


# ------------------------------------------- fleet-wide reset (reset_all)
class FleetRos(FakeRos):
    """Three robots: euclid / hamilton reachable (hamilton's reset fails),
    gauss offline."""

    def __init__(self, base_ok=True):
        super().__init__()
        self.cleared = []
        self.base_ok = base_ok

    def change_detection_unreachable(self, robot):
        return "offline (fleetlink link down)" if robot == "gauss" else None

    def change_detection_cmd(self, robot, cmd):
        out = super().change_detection_cmd(robot, cmd)
        if robot == "hamilton":
            out.update(ok=False, error="no answer from the robot")
        return out

    def clear_change_detection(self, robots):
        self.cleared.append(list(robots))
        self.calls.append(("base", "clear"))
        if not self.base_ok:
            return {"ok": False, "error": "no awcd_base_station clear_robots service on the base station"}
        return {"ok": True, "message": "cleared euclid", "service": "/willow/awcd_base_station/clear_robots"}


class FakeSget:
    def __init__(self, ros, running=True, keep=True, keep_ok=True):
        self.ros, self.running, self.keep, self.keep_ok = ros, running, keep, keep_ok

    def detections_status(self):
        return {"writer": "/willow/scene_change_writer", "running": self.running,
                "keep_supported": self.running and self.keep,
                "reason": None if self.running else "writer not running"}  # fmt: skip

    def keep_detections(self):
        self.ros.calls.append(("writer", "keep_detections"))
        return {"ok": self.keep_ok, "service": "/willow/scene_change_writer/keep_detections",
                "message": "Kept 3 change detection change(s) in the map." if self.keep_ok else "Neo4j down"}  # fmt: skip


def _fleet_client(settings, **sget_kw):
    settings.robots = ["euclid", "gauss", "hamilton"]
    client, svc = make_client(settings)
    return client, svc


def test_reset_all_keeps_map_changes_then_resets_every_robot_and_the_base(settings):
    client, svc = _fleet_client(settings)
    with client:
        ros = FleetRos()
        svc.ros = ros
        svc.chat.sget = FakeSget(ros)
        plan = client.get("/api/change_detection/reset_all").json()
        assert [r["robot"] for r in plan["robots"]] == ["euclid", "gauss", "hamilton"]
        assert [r["reachable"] for r in plan["robots"]] == [True, False, True]
        assert plan["map"]["keep_supported"]
        assert ros.calls == []  # the preview changes nothing

        r = client.post("/api/change_detection/reset_all", json={}).json()
        # the map first: keeping must precede the detectors' empty change sets
        assert ros.calls[0] == ("writer", "keep_detections")
        assert sorted(ros.calls[1:3]) == [("euclid", "reset"), ("hamilton", "reset")]
        assert ros.calls[3] == ("base", "clear") and ros.cleared == [[]]
        assert r["keep_applied"] and r["map"]["action"] == "keep" and r["map"]["ok"]
        by = {x["robot"]: x for x in r["robots"]}
        assert by["euclid"]["ok"] and not by["euclid"]["skipped"]
        assert by["gauss"]["skipped"] and "offline" in by["gauss"]["error"]
        assert not by["hamilton"]["ok"] and not by["hamilton"]["skipped"]
        assert r["base"]["ok"]
        assert r["ok"] is False  # hamilton failed; gauss (offline) alone would not fail it
        events = svc.scene_repair.state()["events"]
        assert events[-1]["kind"] == "detections_reset" and "euclid" in events[-1]["text"]


def test_reset_all_offline_robots_and_no_base_station_are_not_failures(settings):
    client, svc = _fleet_client(settings)
    with client:
        ros = FleetRos(base_ok=False)
        ros.change_detection_cmd = lambda robot, cmd: (ros.calls.append((robot, cmd)), {"ok": True, "message": "reset requested"})[1]  # fmt: skip
        svc.ros = ros
        svc.chat.sget = FakeSget(ros, running=False)
        r = client.post("/api/change_detection/reset_all", json={"keep_applied": True}).json()
        assert r["ok"] and r["map"]["action"] == "none"
        assert ("writer", "keep_detections") not in ros.calls
        assert [x["robot"] for x in r["robots"] if x["skipped"]] == ["gauss"]


def test_reset_all_revert_skips_keep_and_unsupported_keep_is_refused(settings):
    client, svc = _fleet_client(settings)
    with client:
        ros = FleetRos()
        svc.ros = ros
        svc.chat.sget = FakeSget(ros, keep=False)
        r = client.post("/api/change_detection/reset_all", json={})
        assert r.status_code == 409 and r.json()["keep_unsupported"]
        assert ros.calls == []  # nothing reset
        r = client.post("/api/change_detection/reset_all", json={"keep_applied": False}).json()
        assert r["map"]["action"] == "revert" and "reverted" in r["map"]["message"]
        assert ("writer", "keep_detections") not in ros.calls and ("euclid", "reset") in ros.calls


def test_reset_all_aborts_when_keeping_fails(settings):
    client, svc = _fleet_client(settings)
    with client:
        ros = FleetRos()
        svc.ros = ros
        svc.chat.sget = FakeSget(ros, keep_ok=False)
        r = client.post("/api/change_detection/reset_all", json={"robots": ["euclid"]}).json()
        assert not r["ok"] and "Neo4j down" in r["error"]
        assert ros.calls == [("writer", "keep_detections")]  # no robot was reset


def test_reset_all_ros_off_is_503(settings):
    client, svc = make_client(settings)
    with client:
        assert client.post("/api/change_detection/reset_all", json={}).status_code == 503
        assert client.get("/api/change_detection/reset_all").json()["available"] is False


def test_scene_repair_drops_only_detection_recommendations():
    from adt4_console.scene_repair import SceneRepair

    sr = SceneRepair()
    sr._recs["sr1"] = {"id": "sr1", "robot": "euclid", "status": "pending", "source": "awcd/reconciled",
                       "origin": "perception", "changes": [{"source": "awcd/reconciled"}]}  # fmt: skip
    sr._recs["sr2"] = {"id": "sr2", "robot": "hamilton", "status": "pending", "source": "awcd/reconciled",
                       "origin": "perception",
                       "changes": [{"source": "awcd/reconciled"}, {"source": "sget/chat"}]}  # fmt: skip
    assert sr.on_detections_reset("change detection reset") == 1
    assert sr._recs["sr1"]["status"] == "superseded" and sr._recs["sr2"]["status"] == "pending"
    assert "1 pending recommendation" in sr.state()["events"][-1]["text"]


def test_bridge_change_detection_unreachable():
    from adt4_console.ros_bridge import RosBridge

    br = RosBridge(NS(ros=False, planner_ns="willow", ros_domain_id=0))
    links = {"gauss": {"connected": False}, "euclid": {"connected": True, "services": {}},
             "hamilton": {"connected": True, "services": {"/hamilton/change_detection/reset": {}}}}  # fmt: skip
    br.link_status = lambda r: links.get(r)
    br.has_service = lambda name: name == "/phoenix/change_detection/reset"
    assert "offline" in br.change_detection_unreachable("gauss")
    assert "not relayed" in br.change_detection_unreachable("euclid")
    assert br.change_detection_unreachable("hamilton") is None
    assert br.change_detection_unreachable("phoenix") is None  # router mode: served
    assert "no change detection" in br.change_detection_unreachable("sequoia")
    br._plan["sequoia"].change_detection = {"state": "paused", "t": 1.0}
    assert br.change_detection_unreachable("sequoia") is None  # it reported a state
