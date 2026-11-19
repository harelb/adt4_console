"""Wire formats of Penn's quad APIs (API.md §16).

Both are JSON over ZMQ, PUSH → PULL: the client PUSHes a request carrying
``reply_to`` (an endpoint of a PULL socket the client bound) and the server
connects back to it with the answer.

**quad_api** (tasking, default ``:6555``)::

    request  {"op": "task", "x": float, "y": float, "reply_to": str, "req_id": str}
             {"op": "status", "reply_to": str, "req_id": str}
    reply    {"service": "quad_api", "op_status": "ok", "req_id", "time_ns",
              "accepted": bool (task only), "state", "navigator_state",
              "target": {x, y}, "position": {x, y}, "destination": {x, y},
              "distance_m", "waiting_arrival", ...}
    event    pushed later to the same ``reply_to`` when the quad reaches the
             task: an "arrived" message (same ``req_id``)

``x, y`` are metres east / north of the quad's takeoff point.

**camera_geo_api** (images, default ``:5555``)::

    request  {"op": "capture" | "status", "reply_to", "req_id", "require_gps"?: true}
    reply    [json meta, jpeg]  (capture; status has no jpeg frame)
    meta     {"service": "camera_geo_api", "op_status": "ok", "req_id", "time_ns",
              "image": {stamp_ns, stamp_utc, frame_id, format, size_bytes, age_s},
              "gps": {latitude, longitude, altitude_msl, position_covariance},
              "heading_deg", "exif_embedded", "rel_alt_m"}

``heading_deg`` is the flight controller's heading (Penn: do not trust it much).
"""

from __future__ import annotations

import json
import math

QUAD_SERVICE = "quad_api"
CAMERA_SERVICE = "camera_geo_api"


def task_request(x: float, y: float, reply_to: str, req_id: str) -> dict:
    return {
        "op": "task",
        "x": float(x),
        "y": float(y),
        "reply_to": reply_to,
        "req_id": req_id,
    }


def status_request(reply_to: str, req_id: str) -> dict:
    return {"op": "status", "reply_to": reply_to, "req_id": req_id}


def capture_request(reply_to: str, req_id: str, require_gps: bool = False) -> dict:
    req = {"op": "capture", "reply_to": reply_to, "req_id": req_id}
    if require_gps:
        req["require_gps"] = True
    return req


def encode(msg: dict) -> bytes:
    return json.dumps(msg).encode()


def decode(frames: list[bytes]) -> tuple[dict, bytes | None]:
    """A reply/event off the PULL socket: (json, jpeg frame or None)."""
    meta = json.loads(frames[0].decode("utf-8"))
    if not isinstance(meta, dict):
        raise ValueError("reply is not a JSON object")
    return meta, (frames[1] if len(frames) > 1 else None)


def ok(meta: dict) -> bool:
    return meta.get("op_status") == "ok"


def is_arrival(meta: dict) -> bool:
    """An "arrived" event. Penn has not specified its exact shape, so accept the
    common spellings."""
    for k in ("event", "type", "kind", "op", "state", "status"):
        if str(meta.get(k, "")).lower() == "arrived":
            return True
    return meta.get("arrived") is True


def position(meta: dict) -> tuple[float, float] | None:
    p = meta.get("position")
    if isinstance(p, dict) and p.get("x") is not None and p.get("y") is not None:
        return float(p["x"]), float(p["y"])
    return None


def gps(meta: dict) -> dict | None:
    g = meta.get("gps")
    if (
        isinstance(g, dict)
        and g.get("latitude") is not None
        and g.get("longitude") is not None
    ):
        return g
    return None


def heading_to_enu_yaw(heading_deg: float | None) -> float:
    """Compass heading (0 = north, clockwise) → ENU yaw (0 = east, counter-clockwise)."""
    if heading_deg is None:
        return 0.0
    return math.radians(90.0 - float(heading_deg))


class QuadFrame:
    """Map frame ↔ the quad's local frame, via UTM (``GeoAnchor``) and
    heracles_agents' ``UtmToMapInfo`` (``local_utm_origin``, ``map_offset``).

    Penn's frame is metres east / north of takeoff, so ``local_utm_origin`` is the
    takeoff point in UTM and ``map_offset`` is 0. ``anchor`` may be a
    ``GeoAnchor`` or a zero-argument callable returning one (or None), so the
    frame follows the map the console has loaded instead of the one at startup.
    """

    def __init__(self, anchor, local_utm_origin=(0.0, 0.0), map_offset=(0.0, 0.0)):
        self._anchor = anchor
        self.origin = (float(local_utm_origin[0]), float(local_utm_origin[1]))
        self.offset = (float(map_offset[0]), float(map_offset[1]))
        self.origin_source = "flag" if any(self.origin) else None

    @property
    def anchor(self):
        a = self._anchor
        if callable(a) and not hasattr(a, "map_to_utm"):
            try:
                return a()
            except Exception:  # noqa: BLE001 — no anchor is a valid answer
                return None
        return a

    def set_origin(self, e: float, n: float, source: str) -> None:
        self.origin = (float(e), float(n))
        self.offset = (0.0, 0.0)
        self.origin_source = source

    def lonlat_to_utm(self, lon: float, lat: float) -> tuple[float, float] | None:
        a = self.anchor
        if a is None:
            return None
        return a.map_to_utm(*a.lonlat_to_map(lon, lat))

    def lonlat_to_map(self, lon: float, lat: float) -> tuple[float, float] | None:
        a = self.anchor
        return a.lonlat_to_map(lon, lat) if a is not None else None

    def map_to_quad(self, x: float, y: float) -> tuple[float, float]:
        a = self.anchor
        e, n = a.map_to_utm(x, y) if a is not None else (x, y)
        return (
            e - self.origin[0] + self.offset[0],
            n - self.origin[1] + self.offset[1],
        )

    def quad_to_map(self, qx: float, qy: float) -> tuple[float, float]:
        e = qx + self.origin[0] - self.offset[0]
        n = qy + self.origin[1] - self.offset[1]
        a = self.anchor
        return a.utm_to_map(e, n) if a is not None else (e, n)

    def yaw_quad_to_map(self, yaw: float) -> float:
        a = self.anchor
        return yaw - (a.yaw if a is not None else 0.0)

    def yaw_map_to_quad(self, yaw: float) -> float:
        a = self.anchor
        return yaw + (a.yaw if a is not None else 0.0)
