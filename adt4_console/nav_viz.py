"""On-demand robot navigation overlays: the Husky's MPPI / mid-range planner
markers.

They come over ROS on the base station (fleetlink's relay republishes the robot
topics there: ``dcist_launch_system/fleetlink/relay.yaml``). Nothing is
subscribed until the browser asks: ``POST /api/live/<r>/nav_viz`` with
``{group, enabled}`` takes a lease of :data:`LEASE_S` that the page renews
while its toggle is on; an expired lease drops the subscriptions. Updates go to
the hub at most :data:`MIN_PUBLISH_S` apart per robot and group:

* ``robot/<r>/nav_viz`` - ``{layers: {key: {label, frame, t, markers}}, t}``,
  each marker ``{ns, id, kind: strip|segments|points, pts: [[x, y, z]...],
  color: [r, g, b, a] | null}`` in its layer's ``frame`` (the marker pose is
  already applied).

Spot's executor paths (smooth / MLP / target) are not here: the fleetlink agent
already sends them (``robot/<r>/paths``).
"""

from __future__ import annotations

import logging
import math
import threading
import time

log = logging.getLogger(__name__)

LEASE_S = 30.0
MIN_PUBLISH_S = 0.5
REAP_EVERY_S = 5.0
MAX_POINTS_PER_LAYER = 6000

MARKER = "visualization_msgs/msg/Marker"
MARKER_ARRAY = "visualization_msgs/msg/MarkerArray"

# platform -> [(key, label, msg type, topic suffix)] as in phoenix-r2's
# mit_launch/rviz/gauss_mppi_debug.rviz (the relay carries these up)
PLANNER_STREAMS: dict[str, list[tuple[str, str, str, str]]] = {
    "phoenix": [
        ("srp_plan", "MPPI plan", MARKER, "srp_interface/plan"),
        ("srp_reference", "MPPI reference", MARKER, "srp/reference_trajectory"),
        ("srp_debug", "MPPI rollouts", MARKER_ARRAY, "srp_interface/debug"),
        ("mrp_plan", "mid-range plan", MARKER, "mrp_interface/plan"),
    ],
}
GROUPS = ("planner",)

# visualization_msgs/Marker type / action constants
ARROW, CUBE, SPHERE, CYLINDER = 0, 1, 2, 3
LINE_STRIP, LINE_LIST, CUBE_LIST, SPHERE_LIST, POINTS = 4, 5, 6, 7, 8
DELETE, DELETEALL = 2, 3


def planner_streams(platform: str) -> list[tuple[str, str, str, str]]:
    return PLANNER_STREAMS.get("phoenix" if platform == "husky" else platform, [])


# ----------------------------------------------------------------- markers
def _rotate(q, v):
    """Rotate ``v`` by the unit quaternion ``q`` = (x, y, z, w)."""
    x, y, z, w = q
    vx, vy, vz = v
    # t = 2 q_vec x v ; v' = v + w t + q_vec x t
    tx = 2 * (y * vz - z * vy)
    ty = 2 * (z * vx - x * vz)
    tz = 2 * (x * vy - y * vx)
    return (
        vx + w * tx + (y * tz - z * ty),
        vy + w * ty + (z * tx - x * tz),
        vz + w * tz + (x * ty - y * tx),
    )


def _pose_fn(pose):
    p = getattr(pose, "position", None)
    o = getattr(pose, "orientation", None)
    t = (float(p.x), float(p.y), float(p.z)) if p is not None else (0.0, 0.0, 0.0)
    q = (
        (float(o.x), float(o.y), float(o.z), float(o.w))
        if o is not None
        else (0.0, 0.0, 0.0, 1.0)
    )
    n = math.sqrt(sum(c * c for c in q))
    if n < 1e-9:
        q = (0.0, 0.0, 0.0, 1.0)
    else:
        q = tuple(c / n for c in q)
    identity = q == (0.0, 0.0, 0.0, 1.0)

    def apply(v):
        r = v if identity else _rotate(q, v)
        return [round(r[0] + t[0], 3), round(r[1] + t[1], 3), round(r[2] + t[2], 3)]

    return apply, t


def _color(m):
    c = getattr(m, "color", None)
    if c is None:
        return None
    return [
        round(float(c.r), 3),
        round(float(c.g), 3),
        round(float(c.b), 3),
        round(float(c.a), 3),
    ]


def marker_to_dict(m) -> dict | None:
    """One Marker as ``{ns, id, kind, pts, color}`` (pose applied), or None for
    types without a line / point reading (text, meshes, triangles)."""
    typ = int(getattr(m, "type", -1))
    apply, origin = _pose_fn(getattr(m, "pose", None))
    pts_in = [
        (float(p.x), float(p.y), float(p.z)) for p in (getattr(m, "points", None) or [])
    ]
    if typ == LINE_STRIP:
        kind = "strip"
    elif typ == LINE_LIST:
        kind = "segments"
        pts_in = pts_in[: len(pts_in) // 2 * 2]
    elif typ in (POINTS, SPHERE_LIST, CUBE_LIST):
        kind = "points"
    elif typ == ARROW and len(pts_in) >= 2:
        kind = "strip"
        pts_in = pts_in[:2]
    elif typ in (ARROW, CUBE, SPHERE, CYLINDER):
        kind = "points"
        pts_in = [(0.0, 0.0, 0.0)]
    else:
        return None
    if not pts_in:
        return None
    return {
        "ns": str(getattr(m, "ns", "") or ""),
        "id": int(getattr(m, "id", 0) or 0),
        "kind": kind,
        "pts": [apply(p) for p in pts_in],
        "color": _color(m),
    }


def _thin(markers: list[dict], cap: int) -> list[dict]:
    """Keep the layer under ``cap`` points: every k-th point of each marker
    (a strip keeps its ends, segments keep whole pairs)."""
    total = sum(len(m["pts"]) for m in markers)
    if total <= cap:
        return markers
    k = math.ceil(total / cap)
    out = []
    for m in markers:
        pts = m["pts"]
        if m["kind"] == "segments":
            pairs = [pts[i : i + 2] for i in range(0, len(pts), 2)][::k]
            pts = [p for pair in pairs for p in pair]
        elif m["kind"] == "strip":
            kept = pts[::k]
            if kept[-1] is not pts[-1]:
                kept.append(pts[-1])
            pts = kept
        else:
            pts = pts[::k]
        out.append({**m, "pts": pts})
    return out


def layer_from_markers(markers, label: str, cap: int = MAX_POINTS_PER_LAYER) -> dict:
    """A Marker or the markers of a MarkerArray -> ``{label, frame, markers}``.
    Deleted markers are left out; the frame is the first marker's."""
    frame = ""
    out: list[dict] = []
    for m in markers:
        action = int(getattr(m, "action", 0) or 0)
        if action == DELETEALL:
            out = []
            continue
        if action == DELETE:
            key = (str(getattr(m, "ns", "") or ""), int(getattr(m, "id", 0) or 0))
            out = [d for d in out if (d["ns"], d["id"]) != key]
            continue
        if not frame:
            frame = str(getattr(getattr(m, "header", None), "frame_id", "") or "")
        d = marker_to_dict(m)
        if d is not None:
            out.append(d)
    return {"label": label, "frame": frame, "markers": _thin(out, cap)}


# ------------------------------------------------------------- subscriptions
class NavViz:
    """Lease-held ROS subscriptions per (robot, group), throttled to the hub.

    ``subscribe(topic, type, cb)`` / ``unsubscribe(handle)`` are the ROS side
    (``RosBridge.nav_subscribe`` / ``nav_unsubscribe``); tests pass fakes."""

    def __init__(self, subscribe, unsubscribe, platform_of, hub=None,
                 clock=time.monotonic, wall=time.time):  # fmt: skip
        self._subscribe = subscribe
        self._unsubscribe = unsubscribe
        self._platform_of = platform_of
        self.hub = hub
        self.clock = clock
        self.wall = wall
        self._lock = threading.Lock()
        self._leases: dict[tuple[str, str], float] = {}
        self._subs: dict[tuple[str, str], list] = {}
        self._layers: dict[str, dict[str, dict]] = {}
        self._last_pub: dict[tuple[str, str], float] = {}
        self._reaper: threading.Thread | None = None
        self._stop = threading.Event()

    # -------------------------------------------------------------- leases
    def streams(self, robot: str, group: str) -> list[tuple[str, str, str, str]]:
        if group == "planner":
            return planner_streams(self._platform_of(robot))
        raise ValueError(
            f"unknown nav_viz group {group!r} (one of {', '.join(GROUPS)})"
        )

    def set(self, robot: str, group: str, enabled: bool) -> dict:
        streams = self.streams(robot, group)
        key = (robot, group)
        if not enabled:
            with self._lock:
                self._leases.pop(key, None)
            self._drop(key)
            return self.status(robot, group)
        with self._lock:
            self._leases[key] = self.clock() + LEASE_S
            have = key in self._subs
        if not have and streams:
            handles = []
            for skey, label, typ, suffix in streams:
                topic = f"/{robot}/{suffix}"
                cb = self._marker_cb(robot, skey, label, typ)
                try:
                    handles.append(self._subscribe(topic, typ, cb))
                except Exception as e:  # noqa: BLE001 (type not installed)
                    log.warning("nav_viz: cannot subscribe %s (%s): %s", topic, typ, e)
            with self._lock:
                self._subs[key] = handles
        self._ensure_reaper()
        return self.status(robot, group)

    def status(self, robot: str, group: str) -> dict:
        with self._lock:
            exp = self._leases.get((robot, group))
            subscribed = (robot, group) in self._subs
        return {
            "robot": robot,
            "group": group,
            "enabled": exp is not None,
            "subscribed": subscribed,
            "lease_s": round(exp - self.clock(), 1) if exp is not None else None,
            "topics": [f"/{robot}/{s[3]}" for s in self.streams(robot, group)],
        }

    def reap(self) -> None:
        now = self.clock()
        with self._lock:
            dead = [k for k, exp in self._leases.items() if exp <= now]
            for k in dead:
                del self._leases[k]
        for k in dead:
            self._drop(k)

    def _drop(self, key) -> None:
        with self._lock:
            handles = self._subs.pop(key, [])
        for h in handles:
            try:
                self._unsubscribe(h)
            except Exception as e:  # noqa: BLE001
                log.debug("nav_viz unsubscribe: %s", e)
        robot, group = key
        with self._lock:
            self._layers.pop(robot, None)
            self._last_pub.pop(key, None)
        if self.hub is not None:
            self.hub.publish(f"robot/{robot}/nav_viz", None)

    def _ensure_reaper(self) -> None:
        if self._reaper is not None and self._reaper.is_alive():
            return

        def loop():
            while not self._stop.wait(REAP_EVERY_S):
                try:
                    self.reap()
                except Exception:  # noqa: BLE001
                    log.exception("nav_viz reaper")
                with self._lock:
                    if not self._leases and not self._subs:
                        self._reaper = None
                        return

        self._reaper = threading.Thread(target=loop, name="nav-viz-reaper", daemon=True)
        self._reaper.start()

    def close(self) -> None:
        self._stop.set()
        for k in list(self._subs):
            self._drop(k)

    stop = close

    # ------------------------------------------------------------ callbacks
    def _marker_cb(self, robot, key, label, typ):
        many = typ == MARKER_ARRAY

        def cb(msg):
            try:
                markers = list(msg.markers) if many else [msg]
                layer = layer_from_markers(markers, label)
                layer["t"] = self.wall()
                with self._lock:
                    self._layers.setdefault(robot, {})[key] = layer
                self._publish(robot, "planner")
            except Exception:  # noqa: BLE001
                log.exception("nav_viz %s %s", robot, key)

        return cb

    def _publish(self, robot: str, group: str) -> None:
        now = self.clock()
        with self._lock:
            last = self._last_pub.get((robot, group))
            if last is not None and now - last < MIN_PUBLISH_S:
                return
            self._last_pub[(robot, group)] = now
        if self.hub is None:
            return
        self.hub.publish(f"robot/{robot}/nav_viz", self.layers(robot))

    def layers(self, robot: str) -> dict | None:
        with self._lock:
            ls = self._layers.get(robot)
            return {"layers": dict(ls), "t": self.wall()} if ls else None
