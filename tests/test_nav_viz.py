"""On-demand navigation overlays (nav_viz): Husky MPPI / mid-range markers and
the GPS fix, subscribed only while the page holds a lease."""

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


def fix(
    lat=42.3601,
    lon=-71.0942,
    status=2,
    cov=(0.04, 0, 0, 0, 0.09, 0, 0, 0, 0.25),
    cov_type=2,
):
    return NS(latitude=lat, longitude=lon, altitude=12.5,
              status=NS(status=status, service=1), position_covariance=list(cov),
              position_covariance_type=cov_type,
              header=NS(frame_id="gauss/gps", stamp=NS(sec=10, nanosec=500_000_000)))  # fmt: skip


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


# ---------------------------------------------------------------------- GPS
def test_fix_dict_quality():
    d = nv.fix_dict(fix())
    assert d["status_text"] == "GBAS fix (RTK)" and d["std"] == [0.2, 0.3, 0.5]
    assert d["cov_type"] == "diagonal known" and d["stamp"] == 10.5
    assert nv.fix_dict(fix(status=-1, cov_type=0))["std"] is None


def test_origin_utm_env():
    assert nv.origin_utm_from_env({"ADT4_ORIGIN_UTM": "327000.5,4690000.25"}) == (
        327000.5,
        4690000.25,
    )
    assert nv.origin_utm_from_env({"ADT4_ORIGIN_UTM": "nope"}) is None
    assert nv.origin_utm_from_env({}) is None
    assert nv.utm_zone_epsg(42.36, -71.09) == 32619


def test_fix_to_map_origin_utm():
    pytest.importorskip("pyproj")
    from pyproj import Transformer

    e, n = Transformer.from_crs("EPSG:4326", "EPSG:32619", always_xy=True).transform(
        -71.0942, 42.3601
    )
    to_map = nv.FixToMap(geo=None, env={"ADT4_ORIGIN_UTM": f"{e - 10.0},{n + 5.0}"})
    xy, via = to_map(42.3601, -71.0942)
    assert via == "origin_utm" and xy == pytest.approx([10.0, -5.0], abs=1e-3)
    assert nv.FixToMap(geo=None, env={})(42.36, -71.09) == (None, None)


def test_fix_to_map_prefers_the_map_anchor():
    pytest.importorskip("pyproj")
    from adt4_console.geo import GeoAnchor

    anchor = GeoAnchor(32619, 0.0, (327000.0, 4690000.0))
    geo = NS(default_anchor=lambda: anchor)
    xy, via = nv.FixToMap(geo=geo, env={"ADT4_ORIGIN_UTM": "0,0"})(42.3601, -71.0942)
    assert via == "anchor" and xy == pytest.approx(
        list(anchor.lonlat_to_map(-71.0942, 42.3601)), abs=1e-3
    )


# ------------------------------------------------------------------- leases
class Rig:
    def __init__(self, platform="phoenix"):
        self.now = 0.0
        self.subs = {}
        self.dropped = []
        self.published = []
        hub = NS(publish=lambda topic, data: self.published.append((topic, data)))
        self.viz = nv.NavViz(self.subscribe, self.dropped.append, lambda r: platform, hub=hub,
                             to_map=lambda lat, lon: ([1.0, 2.0], "anchor"),
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


def test_spot_has_no_planner_streams_but_has_gps():
    rig = Rig(platform="spot")
    st = rig.viz.set("hamilton", "planner", True)
    assert st["topics"] == [] and rig.subs == {}
    rig.viz.set("hamilton", "gps", True)
    typ, cb = rig.subs["/hamilton/fix"]
    assert typ == nv.NAVSATFIX
    cb(fix())
    topic, data = rig.published[-1]
    assert (
        topic == "robot/hamilton/gps"
        and data["map"] == [1.0, 2.0]
        and data["map_via"] == "anchor"
    )
    rig.viz.set("hamilton", "gps", False)
    assert rig.dropped == ["/hamilton/fix"] and rig.viz.gps("hamilton") is None


def test_unknown_group():
    with pytest.raises(ValueError):
        Rig().viz.set("gauss", "lidar", True)


# -------------------------------------------------------------------- route
def test_route_needs_ros(settings):
    client, svc = make_client(settings)
    with client:
        r = client.post(
            "/api/live/gauss/nav_viz", json={"group": "gps", "enabled": True}
        )
        assert r.status_code == 503
        assert (
            client.post(
                "/api/live/gauss/nav_viz", json={"group": "nope", "enabled": True}
            ).status_code
            == 422
        )
        off = client.post(
            "/api/live/gauss/nav_viz", json={"group": "gps", "enabled": False}
        ).json()
        assert off["enabled"] is False and off["topics"] == ["/gauss/fix"]
        got = client.get("/api/live/gauss/nav_viz").json()
        assert got["planner"] is None and got["gps"] is None
