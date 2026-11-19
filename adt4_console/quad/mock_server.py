"""Mock Penn quadrotor speaking Penn's real JSON APIs (:mod:`.protocol`), for
offline rehearsals and tests.

* binds quad_api (``--quad``): ``task`` → acknowledgement, then flies
  (kinematically) to the point and pushes an ``arrived`` event to the task's
  ``reply_to``; ``status`` → state, position (metres east/north of takeoff);
* binds camera_geo_api (``--camera``): ``capture`` → [meta with GPS,
  rel_alt_m, heading_deg; a nadir JPEG rendered from the satellite tile cache
  with planted mock objects painted in].

Takeoff is the ``--start`` map point.

    python -m adt4_console.quad.mock_server --anchor <run>/geo_anchor.json \\
        --quad tcp://127.0.0.1:6555 --camera tcp://127.0.0.1:5555 \\
        --start 10 -20 --plant crate:35,-60 --plant car:-20,-90
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import threading
import time

from . import camera, protocol

log = logging.getLogger("mock_penn_quad")

ARRIVE_M = 1.0


class MockQuad:
    def __init__(self, anchor, mosaic, planted, start=(0.0, 0.0), speed: float = 4.0,
                 altitude: float = 20.0, gps_covariance: float = 1.4):  # fmt: skip
        self.anchor = anchor
        self.mosaic = mosaic
        self.planted = list(planted)
        self.x, self.y = float(start[0]), float(start[1])  # map frame
        # Penn's frame: metres east/north of takeoff
        if anchor is not None:
            origin = anchor.map_to_utm(self.x, self.y)
        else:
            origin = (self.x, self.y)
        self.frame = protocol.QuadFrame(anchor, origin)
        self.yaw = 0.0  # map frame
        self.altitude = altitude
        self.speed = speed
        self.gps_covariance = gps_covariance
        self.goal = None  # map frame
        self.task = None  # (reply_to, req_id, target quad frame)
        self.arrivals: list[tuple[str, dict]] = []
        self.lock = threading.Lock()

    def position(self) -> dict:
        qx, qy = self.frame.map_to_quad(self.x, self.y)
        return {"x": qx, "y": qy}

    def task_to(self, qx: float, qy: float, reply_to: str, req_id: str) -> dict:
        with self.lock:
            self.goal = self.frame.quad_to_map(qx, qy)
            self.task = (reply_to, req_id, {"x": qx, "y": qy})
            log.info("task quad (%.1f, %.1f) = map (%.1f, %.1f)", qx, qy, *self.goal)
            return self._status_locked(accepted=True)

    def _status_locked(self, **extra) -> dict:
        qx, qy = self.frame.map_to_quad(self.x, self.y)
        target = self.task[2] if self.task else None
        dist = math.dist((qx, qy), (target["x"], target["y"])) if target else 0.0
        return {"service": protocol.QUAD_SERVICE, "op_status": "ok", "time_ns": time.time_ns(),
                "state": "search" if self.goal else "hover",
                "navigator_state": "go_to_target" if self.goal else "idle",
                "target": target, "position": {"x": qx, "y": qy}, "position_age_s": 0.01,
                "destination": target, "distance_m": round(dist, 3),
                "waiting_arrival": self.goal is not None, **extra}  # fmt: skip

    def status(self) -> dict:
        with self.lock:
            return self._status_locked()

    def step(self, dt: float) -> None:
        with self.lock:
            if self.goal is None:
                return
            dx, dy = self.goal[0] - self.x, self.goal[1] - self.y
            d = math.hypot(dx, dy)
            if d < ARRIVE_M:
                self.x, self.y = self.goal
                self.goal = None
                reply_to, req_id, _ = self.task
                self.arrivals.append(
                    (reply_to, self._status_locked(event="arrived", req_id=req_id))
                )
                return
            self.yaw = math.atan2(dy, dx)
            k = min(1.0, self.speed * dt / d)
            self.x += dx * k
            self.y += dy * k

    def capture(self) -> tuple[dict, bytes]:
        with self.lock:
            pose_map = {"x": self.x, "y": self.y, "z": self.altitude, "yaw": self.yaw}
        jpeg, _ = camera.render_nadir(pose_map, self.mosaic, self.planted)
        meta = {"service": protocol.CAMERA_SERVICE, "op_status": "ok", "time_ns": time.time_ns(),
                "image": {"stamp_ns": time.time_ns(), "frame_id": "cam_driver",
                          "format": "bgr8; jpeg compressed bgr8", "size_bytes": len(jpeg), "age_s": 0.02},
                "rel_alt_m": self.altitude, "exif_embedded": False}  # fmt: skip
        utm_yaw = self.frame.yaw_map_to_quad(self.yaw)
        meta["heading_deg"] = (90.0 - math.degrees(utm_yaw)) % 360.0
        if self.anchor is not None:
            lon, lat = self.anchor.map_to_lonlat(self.x, self.y)
            meta["gps"] = {"latitude": lat, "longitude": lon, "altitude_msl": 10.0,
                           "position_covariance": self.gps_covariance}  # fmt: skip
        return meta, jpeg


def serve(
    quad: MockQuad, quad_uri: str, camera_uri: str, stop: threading.Event
) -> None:
    import zmq

    ctx = zmq.Context.instance()
    pulls = {}
    for name, uri in (("quad", quad_uri), ("camera", camera_uri)):
        s = ctx.socket(zmq.PULL)
        s.setsockopt(zmq.LINGER, 0)
        s.bind(uri)
        pulls[s] = name
    pushes: dict[str, object] = {}

    def reply(reply_to: str, frames: list[bytes]) -> None:
        s = pushes.get(reply_to)
        if s is None:
            s = ctx.socket(zmq.PUSH)
            s.setsockopt(zmq.LINGER, 0)
            s.connect(reply_to)
            pushes[reply_to] = s
        s.send_multipart(frames)

    poller = zmq.Poller()
    for s in pulls:
        poller.register(s, zmq.POLLIN)
    last = time.monotonic()
    try:
        while not stop.is_set():
            events = dict(poller.poll(50))
            now = time.monotonic()
            quad.step(now - last)
            last = now
            with quad.lock:
                arrivals, quad.arrivals = quad.arrivals, []
            for reply_to, ev in arrivals:
                reply(reply_to, [json.dumps(ev).encode()])
            for s, name in pulls.items():
                if s not in events:
                    continue
                try:
                    req = json.loads(s.recv().decode())
                    reply_to, rid, op = (
                        req["reply_to"],
                        req.get("req_id"),
                        req.get("op"),
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("bad request: %s", e)
                    continue
                if name == "quad" and op == "task":
                    out = quad.task_to(float(req["x"]), float(req["y"]), reply_to, rid)
                    reply(reply_to, [json.dumps({**out, "req_id": rid}).encode()])
                elif name == "quad" and op == "status":
                    reply(
                        reply_to,
                        [json.dumps({**quad.status(), "req_id": rid}).encode()],
                    )
                elif name == "camera" and op == "capture":
                    meta, jpeg = quad.capture()
                    if req.get("require_gps") and "gps" not in meta:
                        meta = {
                            "service": protocol.CAMERA_SERVICE,
                            "op_status": "no_gps",
                        }
                        reply(reply_to, [json.dumps({**meta, "req_id": rid}).encode()])
                    else:
                        reply(
                            reply_to,
                            [json.dumps({**meta, "req_id": rid}).encode(), jpeg],
                        )
                elif name == "camera" and op == "status":
                    reply(reply_to, [json.dumps({"service": protocol.CAMERA_SERVICE, "op_status": "ok",
                                                 "req_id": rid}).encode()])  # fmt: skip
                else:
                    reply(
                        reply_to,
                        [
                            json.dumps(
                                {"op_status": f"unknown op {op!r}", "req_id": rid}
                            ).encode()
                        ],
                    )
    finally:
        for s in list(pulls) + list(pushes.values()):
            s.close()


def _plant(spec: str) -> dict:
    label, _, xy = spec.partition(":")
    x, y = (float(v) for v in xy.split(","))
    return {"label": label.strip(), "pos": [x, y], "size_m": 1.5}


def main(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--anchor", help="geo_anchor.json of the map (no anchor: no GPS in captures)"
    )
    p.add_argument("--tiles-dir", default="~/.adt4_console/tiles")
    p.add_argument("--layer", default="esri_world_imagery")
    p.add_argument("--zoom", type=int, default=19)
    p.add_argument(
        "--quad", default="tcp://127.0.0.1:6555", help="quad_api PULL endpoint to bind"
    )
    p.add_argument(
        "--camera",
        default="tcp://127.0.0.1:5555",
        help="camera_geo_api PULL endpoint to bind",
    )
    p.add_argument(
        "--start", type=float, nargs=2, default=(0.0, 0.0), help="takeoff, map x y"
    )
    p.add_argument("--speed", type=float, default=4.0)
    p.add_argument("--altitude", type=float, default=20.0)
    p.add_argument(
        "--plant",
        action="append",
        default=[],
        metavar="LABEL:X,Y",
        help="mock object (map frame)",
    )
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")

    from ..geo import GeoAnchor, TileCache

    anchor = GeoAnchor.load(args.anchor) if args.anchor else None
    mosaic = (
        camera.TileMosaic(TileCache(args.tiles_dir), args.layer, anchor, args.zoom)
        if anchor
        else None
    )
    quad = MockQuad(
        anchor,
        mosaic,
        [_plant(s) for s in args.plant],
        args.start,
        args.speed,
        args.altitude,
    )
    stop = threading.Event()
    log.info("mock quad: quad_api on %s, camera_geo_api on %s", args.quad, args.camera)
    try:
        serve(quad, args.quad, args.camera, stop)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
