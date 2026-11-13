"""A map -> UTM anchor estimated live from the robots, for maps without a saved
GPS anchor (``geo_anchor.json``, scripts/geo/anchor_from_gps.py).

Each good GPS fix of a relocalized robot (nav_viz's GPS subscription) is
paired with the robot's map-frame pose at that moment; :func:`geo_fit.fit_anchor`
fits the same rigid map -> UTM transform the offline script fits, or only a
translation (heading unknown) while the pairs are too few or too bunched up.

A saved anchor always wins (GeoService); this one is never written to disk on
its own: :meth:`LiveAnchor.save` writes it only when the operator asks.

Pair gates: fix status >= 0 with a known horizontal 1-sigma <= MAX_SIGMA_M (or
an RTK fix, status 2, with unknown covariance); the robot relocalized (pose in
the global frame); the robot nearly still (the fix and the pose are not
time-matched exactly: at <= MAX_SPEED_MPS the lag error stays small);
>= MIN_SPACING_M from that robot's previous pair. The antenna lever arm is
ignored (tens of cm on a Spot / Husky).
"""

from __future__ import annotations

import logging
import threading
import time

from .geo_fit import fit_anchor

log = logging.getLogger(__name__)

MAX_SIGMA_M = 1.0
MAX_SPEED_MPS = 0.5
MAX_POSE_GAP_S = 3.0
MIN_SPACING_M = 1.0
MAX_PAIRS_PER_ROBOT = 400
FIT_EVERY_S = 5.0
HUB_TOPIC = "geo/live_anchor"


def fix_usable(fix: dict) -> bool:
    status = fix.get("status", -1)
    if status is None or status < 0:
        return False
    std = fix.get("std")
    if std:
        return max(std[0], std[1]) <= MAX_SIGMA_M
    return status >= 2


class LiveAnchor:
    def __init__(self, hub=None, clock=time.monotonic, wall=time.time, utm_of=None):
        self.hub = hub
        self.clock = clock
        self.wall = wall
        self._utm_of = utm_of or _utm_of
        self._lock = threading.Lock()
        # robot -> [(map_x, map_y, utm_e, utm_n, epsg)]
        self._pairs: dict[str, list[tuple]] = {}
        # robot -> (clock, x, y) of the previous pose seen with a fix
        self._last_pose: dict[str, tuple[float, float, float]] = {}
        self._anchor: dict | None = None
        self._dirty = False
        self._last_fit = -1e9
        self._rejects: dict[str, int] = {}

    # ---------------------------------------------------------------- input
    def add(self, robot: str, fix: dict, pose_xy) -> bool:
        """Offer one fix with the robot's global-frame (x, y) (None: not
        relocalized). Returns True when it became a pair."""
        now = self.clock()
        prev = self._last_pose.get(robot)
        if pose_xy is None:
            self._last_pose.pop(robot, None)
            return self._reject(robot, "not relocalized")
        x, y = float(pose_xy[0]), float(pose_xy[1])
        self._last_pose[robot] = (now, x, y)
        if not fix_usable(fix):
            return self._reject(robot, "fix quality")
        if prev is None or now - prev[0] > MAX_POSE_GAP_S:
            return self._reject(robot, "no speed yet")
        dt = max(now - prev[0], 1e-3)
        if ((x - prev[1]) ** 2 + (y - prev[2]) ** 2) ** 0.5 / dt > MAX_SPEED_MPS:
            return self._reject(robot, "moving")
        try:
            e, n, epsg = self._utm_of(fix["lat"], fix["lon"])
        except Exception as ex:  # noqa: BLE001 (pyproj missing)
            log.debug("live anchor: no UTM: %s", ex)
            return False
        with self._lock:
            pairs = self._pairs.setdefault(robot, [])
            if (
                pairs
                and ((pairs[-1][0] - x) ** 2 + (pairs[-1][1] - y) ** 2) ** 0.5
                < MIN_SPACING_M
            ):
                return False
            pairs.append((x, y, e, n, epsg))
            del pairs[:-MAX_PAIRS_PER_ROBOT]
            self._dirty = True
        self.maybe_fit()
        return True

    def _reject(self, robot: str, why: str) -> bool:
        self._rejects[why] = self._rejects.get(why, 0) + 1
        return False

    # ------------------------------------------------------------------ fit
    def maybe_fit(self, force: bool = False) -> dict | None:
        now = self.clock()
        with self._lock:
            if not force and (not self._dirty or now - self._last_fit < FIT_EVERY_S):
                return self._anchor
            self._dirty = False
            self._last_fit = now
            pairs = {r: list(p) for r, p in self._pairs.items() if p}
        anchor = self._fit(pairs)
        with self._lock:
            self._anchor = anchor
        if self.hub is not None:
            self.hub.publish(HUB_TOPIC, anchor)
        return anchor

    def _fit(self, pairs: dict[str, list[tuple]]) -> dict | None:
        rows = [(r, p) for r, ps in pairs.items() for p in ps]
        if not rows:
            return None
        # one UTM zone: the most common one (a fleet does not straddle zones)
        zones: dict[int, int] = {}
        for _, p in rows:
            zones[p[4]] = zones.get(p[4], 0) + 1
        epsg = max(zones, key=zones.get)
        rows = [(r, p) for r, p in rows if p[4] == epsg]
        f = fit_anchor([p[:2] for _, p in rows], [p[2:4] for _, p in rows])
        if f is None:
            return None
        robots = sorted({r for r, _ in rows})
        return {
            **f,
            "epsg": epsg,
            "z0": 0.0,
            "live": True,
            "robots": robots,
            "per_robot": {r: sum(1 for rr, _ in rows if rr == r) for r in robots},
            "fitted_at": self.wall(),
        }

    def anchor(self) -> dict | None:
        """The current live anchor (GeoAnchor fields + fit stats) or None."""
        with self._lock:
            return dict(self._anchor) if self._anchor else None

    def status(self) -> dict:
        with self._lock:
            return {
                "anchor": dict(self._anchor) if self._anchor else None,
                "pairs": {r: len(p) for r, p in self._pairs.items()},
                "rejected": dict(self._rejects),
            }

    def reset(self) -> None:
        with self._lock:
            self._pairs.clear()
            self._last_pose.clear()
            self._rejects.clear()
            self._anchor = None
            self._dirty = False
        if self.hub is not None:
            self.hub.publish(HUB_TOPIC, None)


def _utm_of(lat: float, lon: float) -> tuple[float, float, int]:
    from .geo import _transformer
    from .nav_viz import utm_zone_epsg

    epsg = utm_zone_epsg(lat, lon)
    e, n = _transformer(epsg, inverse=True).transform(lon, lat)
    return float(e), float(n), epsg
