"""On-demand navigation overlays (nav_viz): Husky MPPI / mid-range markers,
subscribed only while the page holds a lease."""

from types import SimpleNamespace as NS

import pytest

from adt4_console import nav_viz as nv

from .conftest import make_client


def V(x, y, z=0.0):
    return NS(x=x, y=y, z=z)


def pose(x=0.0, y=0.0, z=0.0, qx=0.0, qy=0.0, qz=0.0, qw=1.0):
    return NS(position=V(x, y, z), orientation=NS(x=qx, y=qy, z=qz, w=qw))


def marker(
    typ,
    pts,
    frame="gauss/odom",
    ns="plan",
    id=0,
    action=0,
    p=None,
    color=(1, 0.5, 0, 1),
):
    return NS(header=NS(frame_id=frame), ns=ns, id=id, type=typ, action=action,
              pose=p or pose(), points=[V(*q) for q in pts],
              color=NS(r=color[0], g=color[1], b=color[2], a=color[3]))  # fmt: skip


# ------------------------------------------------------------------ markers
def test_line_strip_applies_marker_pose():
    # 90 deg yaw about z, then +1 m in x
    m = marker(
        nv.LINE_STRIP, [(1, 0, 0), (2, 0, 0)], p=pose(x=1.0, qz=2**-0.5, qw=2**-0.5)
    )
    d = nv.marker_to_dict(m)
    assert d["kind"] == "strip" and d["color"] == [1.0, 0.5, 0.0, 1.0]
    assert d["pts"] == [[1.0, 1.0, 0.0], [1.0, 2.0, 0.0]]


def test_marker_kinds():
    assert nv.marker_to_dict(marker(nv.LINE_LIST, [(0, 0, 0), (1, 0, 0), (2, 0, 0)]))["pts"] == [
        [0.0, 0.0, 0.0], [1.0, 0.0, 0.0]]  # fmt: skip
    assert nv.marker_to_dict(marker(nv.POINTS, [(0, 0, 0)]))["kind"] == "points"
    sphere = nv.marker_to_dict(marker(nv.SPHERE, [], p=pose(3, 4, 0)))
    assert sphere["kind"] == "points" and sphere["pts"] == [[3.0, 4.0, 0.0]]
    assert nv.marker_to_dict(marker(9, [(0, 0, 0)])) is None  # TEXT_VIEW_FACING
    assert nv.marker_to_dict(marker(nv.LINE_STRIP, [])) is None


def test_layer_handles_delete_and_frame():
    ms = [
        marker(nv.LINE_STRIP, [(0, 0, 0), (1, 0, 0)], id=1),
        marker(nv.LINE_STRIP, [(0, 0, 0), (2, 0, 0)], id=2),
        marker(nv.LINE_STRIP, [], id=1, action=nv.DELETE),
    ]
    layer = nv.layer_from_markers(ms, "MPPI rollouts")
    assert layer["frame"] == "gauss/odom" and [m["id"] for m in layer["markers"]] == [2]
    reset = nv.layer_from_markers(
        [ms[0], marker(nv.LINE_STRIP, [], action=nv.DELETEALL)], "x"
    )
    assert reset["markers"] == []


def test_layer_is_thinned_under_the_cap():
    pts = [(float(i), 0.0, 0.0) for i in range(1000)]
    layer = nv.layer_from_markers(
        [marker(nv.LINE_STRIP, pts, id=i) for i in range(10)], "x", cap=1000
    )
    total = sum(len(m["pts"]) for m in layer["markers"])
    assert total <= 1000 + 10
    assert all(m["pts"][-1] == [999.0, 0.0, 0.0] for m in layer["markers"])  # ends kept


# ------------------------------------------------------------------- leases
class Rig:
    def __init__(self, platform="phoenix"):
        self.now = 0.0
        self.subs = {}
        self.dropped = []
        self.published = []
        hub = NS(publish=lambda topic, data: self.published.append((topic, data)))
        self.viz = nv.NavViz(self.subscribe, self.dropped.append, lambda r: platform, hub=hub,
                             clock=lambda: self.now, wall=lambda: 100.0)  # fmt: skip

    def subscribe(self, topic, typ, cb):
        self.subs[topic] = (typ, cb)
        return topic


def test_planner_lease_subscribes_publishes_and_expires():
    rig = Rig()
    st = rig.viz.set("gauss", "planner", True)
    assert st["enabled"] and st["subscribed"]
    assert set(rig.subs) == {"/gauss/srp_interface/plan", "/gauss/srp/reference_trajectory",
                             "/gauss/srp_interface/debug", "/gauss/mrp_interface/plan"}  # fmt: skip
    typ, cb = rig.subs["/gauss/srp_interface/debug"]
    assert typ == nv.MARKER_ARRAY
    cb(NS(markers=[marker(nv.LINE_STRIP, [(0, 0, 0), (1, 1, 0)], ns="rollouts")]))
    topic, data = rig.published[-1]
    assert topic == "robot/gauss/nav_viz"
    assert data["layers"]["srp_debug"]["label"] == "MPPI rollouts"
    # throttled: a second message within MIN_PUBLISH_S is stored, not pushed
    rig.subs["/gauss/srp_interface/plan"][1](
        marker(nv.LINE_STRIP, [(0, 0, 0), (2, 0, 0)])
    )
    assert len(rig.published) == 1 and "srp_plan" in rig.viz.layers("gauss")["layers"]
    # renewing keeps the subscriptions; expiry drops them and clears the view
    rig.now = nv.LEASE_S - 1
    rig.viz.set("gauss", "planner", True)
    rig.now = nv.LEASE_S + 5
    rig.viz.reap()
    assert rig.dropped == [] and rig.viz.status("gauss", "planner")["subscribed"]
    rig.now = 2 * nv.LEASE_S
    rig.viz.reap()
    assert len(rig.dropped) == 4 and rig.published[-1] == ("robot/gauss/nav_viz", None)
    assert rig.viz.layers("gauss") is None


def test_spot_has_no_planner_streams():
    rig = Rig(platform="spot")
    st = rig.viz.set("hamilton", "planner", True)
    assert st["topics"] == [] and rig.subs == {}


def test_unknown_group():
    with pytest.raises(ValueError):
        Rig().viz.set("gauss", "gps", True)


# -------------------------------------------------------------------- route
def test_route_needs_ros(settings):
    client, svc = make_client(settings)
    with client:
        r = client.post(
            "/api/live/gauss/nav_viz", json={"group": "planner", "enabled": True}
        )
        assert r.status_code == 503
        bad = client.post(
            "/api/live/gauss/nav_viz", json={"group": "nope", "enabled": True}
        )
        assert bad.status_code == 422
        off = client.post(
            "/api/live/gauss/nav_viz", json={"group": "planner", "enabled": False}
        ).json()
        assert off["enabled"] is False
        assert client.get("/api/live/gauss/nav_viz").json()["planner"] is None
