"""Live map -> UTM anchor from the robots' GPS fixes + relocalized poses
(geo_fit.fit_anchor, live_anchor.LiveAnchor) and its use as the fallback for
maps without a saved anchor."""

import json
import math

import numpy as np
import pytest

from adt4_console import geo_fit, live_anchor

YAW = math.radians(23.0)
T = np.array([327000.0, 4690000.0])


def to_utm(mp, yaw=YAW, t=T):
    c, s = math.cos(yaw), math.sin(yaw)
    R = np.array([[c, -s], [s, c]])
    return (R @ np.asarray(mp, float).T).T + t


def square_walk(n=40, side=20.0):
    k = np.linspace(0, 4, n, endpoint=False)
    pts = []
    for v in k:
        i, f = int(v), v - int(v)
        corners = [(0, 0), (side, 0), (side, side), (0, side), (0, 0)]
        a, b = np.array(corners[i]), np.array(corners[i + 1])
        pts.append(a + f * (b - a))
    return np.array(pts)


# --------------------------------------------------------------------- fit
def test_rigid_fit_recovers_yaw_and_translation():
    rng = np.random.default_rng(0)
    mp = square_walk()
    up = to_utm(mp) + rng.normal(0, 0.05, (len(mp), 2))
    f = geo_fit.fit_anchor(mp, up)
    assert f["mode"] == "rigid" and f["heading_known"]
    assert math.degrees(f["yaw"]) == pytest.approx(23.0, abs=0.5)
    assert f["t"] == pytest.approx(T.tolist(), abs=0.3)
    assert f["rms"] < 0.15 and f["n"] == len(mp)


def test_rigid_fit_rejects_an_outlier():
    mp = square_walk()
    up = to_utm(mp)
    up[5] += [15.0, -12.0]  # a multipath jump
    f = geo_fit.fit_anchor(mp, up)
    assert f["n"] == len(mp) - 1 and f["rms"] < 1e-6
    assert math.degrees(f["yaw"]) == pytest.approx(23.0, abs=1e-6)


def test_short_baseline_is_translation_only():
    # robot standing within ~1 m: heading cannot be observed
    rng = np.random.default_rng(1)
    mp = rng.normal(0, 0.3, (20, 2)) + [5.0, 5.0]
    up = to_utm(mp, yaw=0.0) + rng.normal(0, 0.05, (20, 2))
    f = geo_fit.fit_anchor(mp, up)
    assert f["mode"] == "translation" and not f["heading_known"] and f["yaw"] == 0.0
    assert f["t"] == pytest.approx(T.tolist(), abs=0.1)


def test_too_few_pairs_for_rigid_and_for_anything():
    mp = np.array([[0.0, 0.0], [30.0, 0.0], [0.0, 30.0]])
    f = geo_fit.fit_anchor(mp, to_utm(mp))
    assert f["mode"] == "translation"  # 3 pairs < MIN_RIGID_PAIRS even if spread
    assert geo_fit.fit_anchor(mp[:2], to_utm(mp[:2])) is None
    assert geo_fit.fit_anchor(np.zeros((0, 2)), np.zeros((0, 2))) is None


def test_straight_line_is_enough_for_yaw():
    mp = np.c_[np.linspace(0, 20, 15), np.zeros(15)]
    f = geo_fit.fit_anchor(mp, to_utm(mp))
    assert f["mode"] == "rigid" and math.degrees(f["yaw"]) == pytest.approx(
        23.0, abs=1e-6
    )


def test_nan_pairs_are_dropped():
    mp = square_walk()
    up = to_utm(mp)
    up[0] = [np.nan, np.nan]
    assert geo_fit.fit_anchor(mp, up)["n_pairs"] == len(mp) - 1


# ------------------------------------------------------------- live anchor
class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def utm_fixed(lat, lon):
    # tests feed UTM through lat/lon slots: lat = easting, lon = northing
    return float(lat), float(lon), 32619


def fix(e, n, status=2, std=(0.05, 0.05, 0.1)):
    return {"lat": e, "lon": n, "status": status, "std": list(std) if std else None}


def drive(la, clock, robot, mp, *, dt=4.0, still=True, **kw):
    """Robot stops at each waypoint (two fixes 1 s apart: the second pairs)."""
    up = to_utm(mp)
    for (x, y), (e, n) in zip(mp, up):
        clock.t += dt
        la.add(robot, fix(e, n, **kw), (x, y))
        clock.t += 1.0
        la.add(robot, fix(e, n, **kw), (x, y) if still else (x + 3.0, y))


def test_live_anchor_accumulates_and_fits():
    clock, pub = Clock(), []
    hub = type("H", (), {"publish": lambda self, t, d: pub.append((t, d))})()
    la = live_anchor.LiveAnchor(hub, clock=clock, wall=lambda: 1.0, utm_of=utm_fixed)
    drive(la, clock, "gauss", square_walk(30))
    a = la.maybe_fit(force=True)
    assert a["mode"] == "rigid" and a["robots"] == ["gauss"] and a["epsg"] == 32619
    assert math.degrees(a["yaw"]) == pytest.approx(23.0, abs=1e-3)
    assert pub[-1] == ("geo/live_anchor", a)
    assert la.status()["pairs"] == {"gauss": 30}


def test_live_anchor_gates():
    clock = Clock()
    la = live_anchor.LiveAnchor(None, clock=clock, utm_of=utm_fixed)
    mp = square_walk(10)
    drive(la, clock, "euclid", mp, still=False)  # moving: 3 m in 1 s
    drive(la, clock, "euclid", mp, status=0, std=(3.0, 3.0, 5.0))  # poor fix
    drive(la, clock, "euclid", mp, status=0, std=None)  # unknown covariance, no RTK
    assert la.status()["pairs"] == {}
    rej = la.status()["rejected"]
    assert rej["moving"] and rej["fix quality"]
    clock.t += 1
    assert la.add("euclid", fix(*to_utm([[0, 0]])[0]), None) is False  # not relocalized
    assert la.status()["rejected"]["not relocalized"] == 1


def test_live_anchor_spacing_and_reset():
    clock = Clock()
    la = live_anchor.LiveAnchor(None, clock=clock, utm_of=utm_fixed)
    for _ in range(10):  # standing still: one pair only
        clock.t += 1.0
        la.add("hamilton", fix(*T), (0.0, 0.0))
    assert la.status()["pairs"] == {"hamilton": 1}
    assert la.maybe_fit(force=True) is None  # 1 pair < MIN_PAIRS
    la.reset()
    assert la.status() == {"anchor": None, "pairs": {}, "rejected": {}}


def test_fix_usable():
    assert live_anchor.fix_usable({"status": 2, "std": None})
    assert not live_anchor.fix_usable({"status": 0, "std": None})
    assert live_anchor.fix_usable({"status": 0, "std": [0.4, 0.6, 1.0]})
    assert not live_anchor.fix_usable({"status": -1, "std": [0.1, 0.1, 0.1]})


# ------------------------------------------------------------ geo service
class FakeLive:
    def __init__(self, a):
        self.a = a

    def anchor(self):
        return self.a


LIVE = {"epsg": 32619, "yaw": 0.1, "t": [327000.0, 4690000.0], "z0": 0.0, "rms": 0.3,
        "n": 25, "mode": "rigid", "heading_known": True, "robots": ["gauss"], "baseline_m": 12.0}  # fmt: skip


def test_saved_anchor_wins_and_live_is_the_fallback(tmp_path):
    from adt4_console.geo import ANCHOR_FILE, GeoService

    run = tmp_path / "run"
    run.mkdir()
    src = type("S", (), {"run_dir": str(run)})()
    graph = type(
        "G", (), {"get": lambda self, i: src, "list": lambda self: [{"id": "file:x"}]}
    )()
    geo = GeoService(type("St", (), {"tiles_dir": str(tmp_path / "tiles")})(), graph)
    assert geo.effective_anchor("file:x") == (None, None)
    geo.live = FakeLive(dict(LIVE))
    a, info = geo.effective_anchor("file:x")
    assert a.yaw == 0.1 and info["mode"] == "rigid"
    # explicit save writes geo_anchor.json; from then on the saved one is used
    path = geo.save_live_anchor("file:x")
    assert path == run / ANCHOR_FILE and json.loads(path.read_text())["source"][
        "robots"
    ] == ["gauss"]
    geo.live = FakeLive({**LIVE, "yaw": 0.5})
    a, info = geo.effective_anchor("file:x")
    assert info is None and a.yaw == pytest.approx(0.1)
    with pytest.raises(FileExistsError):
        geo.save_live_anchor("file:x")


def test_save_refuses_without_live_anchor(tmp_path):
    from adt4_console.geo import GeoService

    src = type("S", (), {"run_dir": str(tmp_path)})()
    graph = type("G", (), {"get": lambda self, i: src, "list": lambda self: []})()
    geo = GeoService(type("St", (), {"tiles_dir": str(tmp_path / "tiles")})(), graph)
    with pytest.raises(ValueError):
        geo.save_live_anchor("file:x")
    geo.live = FakeLive(None)
    with pytest.raises(ValueError):
        geo.save_live_anchor("file:x")


def test_fix_to_map_uses_live_last():
    pytest.importorskip("pyproj")
    from adt4_console.nav_viz import FixToMap

    to_map = FixToMap(geo=None, env={}, live=FakeLive(dict(LIVE, yaw=0.0)))
    xy, via = to_map(42.3601, -71.0942)
    assert via == "live" and xy is not None
    xy2, via2 = FixToMap(
        geo=None, env={"ADT4_ORIGIN_UTM": "327000,4690000"}, live=FakeLive(dict(LIVE))
    )(42.3601, -71.0942)
    assert via2 == "origin_utm" and xy2 == pytest.approx(
        xy, abs=1e-6
    )  # same origin, yaw 0
