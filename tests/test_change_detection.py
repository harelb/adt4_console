"""Change detection start / stop / reset (API.md §19): the relocalization gate."""

from adt4_console.routes.change_detection import relocalized

from .conftest import make_client

FA_OK = {
    "parent": "map",
    "child": "euclid/odom",
    "ok": True,
    "T": [0, 0, 0, 1, 0, 0, 0],
}


def test_relocalized_verdicts():
    assert not relocalized(None)["ok"]
    assert "older agent" in relocalized({"pose": {}})["reason"]
    assert not relocalized({"frame_align": {**FA_OK, "ok": False}})["ok"]
    assert not relocalized({"frame_align": {**FA_OK, "child": "map"}})["ok"]
    assert relocalized({"frame_align": FA_OK})[
        "ok"
    ]  # no ROMAN info: the transform decides
    no_match = relocalized({"frame_align": FA_OK, "reloc": {"prior_lcs": 0}})
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
    cs.on_robot(awcd_msg_to_dict(_msg("euclid", removed=[_info(42, "cone", (3, 3, 0))])))
    cs.on_robot(awcd_msg_to_dict(_msg("reconciled")))  # ignored
    added_id = (namespace_crc("hamilton") << 32) | 5
    rec = _msg("reconciled", added=[_info(added_id, "bag", (0, 0, 0))],
               removed=[_info(42, "cone", (3, 3, 0))])  # fmt: skip
    cs.on_reconciled(awcd_msg_to_dict(rec), "/willow/awcd_base_station/reconciled_changes")
    st = cs.state(["euclid", "hamilton", "gauss"])
    assert st["topic"].endswith("reconciled_changes") and st["t"] == 100.0
    assert st["counts"] == {"added": 1, "removed": 1}
    removed, added = st["changes"]
    assert removed["kind"] == "removed" and removed["robots"] == ["euclid", "hamilton"]
    assert added["kind"] == "added" and added["robots"] == ["hamilton"]
    assert added["key"] == f"a{added_id}" and added["label"] == "bag" and added["short_id"] == 5
    assert set(st["robots"]) == {"euclid", "hamilton"}
    assert st["robots"]["hamilton"] == {"t": 100.0, "added": 1, "removed": 1}
    # a base station clear: robots' reports dropped; the reconciled set follows
    # when the base station republishes it (an empty set here)
    cs.forget(["euclid"])
    assert cs.state()["changes"][0]["robots"] == ["hamilton"]
    cs.on_reconciled(awcd_msg_to_dict(_msg("reconciled")))
    assert cs.state()["changes"] == [] and cs.state()["topic"].endswith("reconciled_changes")
    cs.forget()
    assert cs.state()["robots"] == {}


def test_changes_route(settings):
    client, svc = make_client(settings)
    with client:
        # ROS off: an empty, unavailable set (the UI hides the overlay)
        r = client.get("/api/change_detection/changes").json()
        assert r["available"] is False and r["changes"] == []
        cs = AwcdChangeSet()
        cs.on_reconciled(awcd_msg_to_dict(_msg("reconciled", removed=[_info(1, "cone", (0, 0, 0))])))
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
    assert last["changes"][0]["robots"] == ["hamilton"] and last["counts"]["removed"] == 1
    calls = []
    br.run = lambda fn, timeout=5.0: [("/willow/awcd_base_station/clear_robots", ["x"])]
    br.call_service = lambda name, typ, req, timeout=0: calls.append((name, req)) or {"success": True}
    out = br.clear_change_detection(["hamilton"])
    assert out["ok"] and calls == [("/willow/awcd_base_station/clear_robots", {"robot_names": ["hamilton"]})]
    assert br.awcd_changes()["robots"] == {}
