"""The console side of Penn's quad APIs (API.md §16, wire formats in :mod:`.protocol`).

One PULL socket, bound once on ``reply_host``, receives every reply and event;
a receiver thread owns it and routes messages by ``req_id``:

* the first message for a pending request answers it (``status``, ``capture``,
  the ``task`` acknowledgement);
* anything else (the ``arrived`` event a task sends later, stray replies) goes
  to the listeners registered with :meth:`QuadLink.on_event`.

Requests go out on one long-lived PUSH socket per server, with ``IMMEDIATE`` so
a send to a server that is not there fails at once instead of queueing.
"""

from __future__ import annotations

import io
import itertools
import logging
import math
import socket
import threading
import time
from urllib.parse import urlparse

from . import camera, protocol

log = logging.getLogger(__name__)

MAX_GPS_COVARIANCE = 25.0  # m², calibration rejects worse fixes
MAX_IMAGE_AGE_S = 1.0
MAX_CALIBRATION_MOVE_M = 1.0


class QuadUnavailable(Exception):
    pass


def local_ip_toward(uri: str | None) -> str:
    """The address of this machine on the route to ``uri``'s host (no packet is sent)."""
    host = urlparse(uri or "").hostname if uri else None
    if not host or host in ("*", "0.0.0.0", "localhost") or host.startswith("127."):
        return "127.0.0.1"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def jpeg_size(jpeg: bytes) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(jpeg)) as im:
            return im.size
    except Exception:  # noqa: BLE001
        return camera.WIDTH, camera.HEIGHT


class QuadLink:
    def __init__(self, cmd_uri: str | None, image_uri: str | None, frame: protocol.QuadFrame,
                 reply_host: str | None = None, timeout_s: float = 5.0,
                 hfov_deg: float = camera.HFOV_DEG, altitude_m: float = 20.0):  # fmt: skip
        self.cmd_uri = cmd_uri  # quad_api PULL endpoint (Penn's server)
        self.image_uri = image_uri  # camera_geo_api PULL endpoint
        self.frame = frame
        self.reply_host = reply_host or local_ip_toward(cmd_uri or image_uri)
        self.timeout_s = timeout_s
        self.connect_wait_s = 1.0
        self.hfov_deg = hfov_deg
        self.altitude_m = altitude_m  # used when a capture has no rel_alt_m
        self._lock = threading.Lock()  # PUSH sockets
        self._ctx = None
        self._push: dict[str, object] = {}
        self._reply_to: str | None = None
        self._rx: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._pending: dict[str, list] = {}  # req_id -> [Event, (meta, jpeg) | None]
        self._pending_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._listeners: list = []
        self.last_status: dict | None = None
        self.last_image: dict | None = None  # header of the newest capture
        self.last_command: dict | None = None
        self.last_error: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.cmd_uri or self.image_uri)

    # ------------------------------------------------------------ lifecycle
    def _context(self):
        if self._ctx is None:
            try:
                import zmq
            except ImportError as e:
                raise QuadUnavailable(f"pyzmq unavailable: {e}") from None
            self._ctx = zmq.Context.instance()
        return self._ctx

    def start(self) -> None:
        """Bind the reply socket and start the receiver (idempotent, never raises)."""
        if not self.configured or (self._rx is not None and self._rx.is_alive()):
            return
        try:
            self._context()
        except QuadUnavailable as e:
            log.warning("quad link: %s", e)
            return
        self._stop.clear()
        self._ready.clear()
        self._rx = threading.Thread(target=self._receive, name="quad-rx", daemon=True)
        self._rx.start()
        if not self._ready.wait(2.0):
            log.warning("quad link: reply socket on %s not ready", self.reply_host)

    def stop(self) -> None:
        self._stop.set()
        if self._rx is not None:
            self._rx.join(timeout=2.0)
        self._rx = None
        with self._lock:
            for s in self._push.values():
                s.close()
            self._push.clear()

    close = stop

    def on_event(self, fn) -> None:
        """``fn(meta, jpeg)`` for every message that answers no pending request."""
        self._listeners.append(fn)

    def _receive(self) -> None:
        import zmq

        pull = self._context().socket(zmq.PULL)
        pull.setsockopt(zmq.LINGER, 0)
        try:
            pull.bind(f"tcp://{self.reply_host}:*")
        except zmq.ZMQError as e:
            self.last_error = f"cannot bind the reply socket on {self.reply_host}: {e}"
            log.error("quad link: %s", self.last_error)
            pull.close()
            self._ready.set()
            return
        # LAST_ENDPOINT resolves '*' to 0.0.0.0 for a wildcard host: not connectable
        self._reply_to = (
            pull.getsockopt(zmq.LAST_ENDPOINT)
            .decode()
            .replace("0.0.0.0", self.reply_host)
        )
        log.info("quad link: replies on %s", self._reply_to)
        self._ready.set()
        try:
            while not self._stop.is_set():
                if not pull.poll(100, zmq.POLLIN):
                    continue
                frames = pull.recv_multipart()
                try:
                    meta, jpeg = protocol.decode(frames)
                except Exception as e:  # noqa: BLE001
                    log.warning("quad link: undecodable message: %s", e)
                    continue
                self._route(meta, jpeg)
        finally:
            pull.close()
            self._reply_to = None

    def _route(self, meta: dict, jpeg: bytes | None) -> None:
        rid = str(meta.get("req_id", ""))
        with self._pending_lock:
            slot = self._pending.pop(rid, None)
        if slot is not None:
            slot[1] = (meta, jpeg)
            slot[0].set()
            return
        for fn in list(self._listeners):
            try:
                fn(meta, jpeg)
            except Exception:  # noqa: BLE001
                log.exception("quad event listener")

    # ------------------------------------------------------------- requests
    def _push_for(self, uri: str):
        import zmq

        s = self._push.get(uri)
        if s is None:
            s = self._context().socket(zmq.PUSH)
            s.setsockopt(zmq.IMMEDIATE, 1)  # no queueing to a server that is not there
            s.setsockopt(zmq.LINGER, 0)
            s.connect(uri)
            self._push[uri] = s
        return s

    def _request(
        self, uri: str | None, build, what: str, timeout_s: float | None = None
    ):
        if not uri:
            raise QuadUnavailable(f"no {what} endpoint configured")
        import zmq

        self.start()
        if self._reply_to is None:
            raise QuadUnavailable(self.last_error or "quad link reply socket not bound")
        rid = f"adt4-{next(self._ids)}"
        msg = build(self._reply_to, rid)
        slot = [threading.Event(), None]
        with self._pending_lock:
            self._pending[rid] = slot
        try:
            with self._lock:
                push = self._push_for(uri)
                if not push.poll(int(self.connect_wait_s * 1000), zmq.POLLOUT):
                    raise QuadUnavailable(f"{what} at {uri} is not reachable")
                try:
                    push.send(protocol.encode(msg), flags=zmq.NOBLOCK)
                except zmq.Again:
                    raise QuadUnavailable(f"{what} at {uri} is not reachable") from None
            if not slot[0].wait(timeout_s or self.timeout_s):
                raise QuadUnavailable(
                    f"{what} at {uri} did not answer within {timeout_s or self.timeout_s:.0f} s"
                )
        except QuadUnavailable as e:
            self.last_error = str(e)
            raise
        finally:
            with self._pending_lock:
                self._pending.pop(rid, None)
        meta, jpeg = slot[1]
        if not protocol.ok(meta):
            self.last_error = f"{what}: {meta.get('op_status')} {meta.get('error') or meta.get('message') or ''}".strip()
            raise QuadUnavailable(self.last_error)
        self.last_error = None
        return msg, meta, jpeg

    def _with_map_position(self, meta: dict) -> dict:
        p = protocol.position(meta)
        if p is not None:
            mx, my = self.frame.quad_to_map(*p)
            meta["position_map"] = {"x": mx, "y": my}
        for k in ("target", "destination"):
            q = meta.get(k)
            if isinstance(q, dict) and q.get("x") is not None:
                mx, my = self.frame.quad_to_map(float(q["x"]), float(q["y"]))
                meta[f"{k}_map"] = {"x": mx, "y": my}
        return meta

    def status(self) -> dict:
        """quad_api status, with ``position_map`` (and ``target_map``…) added."""
        _, meta, _ = self._request(self.cmd_uri, protocol.status_request, "quad_api")
        meta = self._with_map_position(meta)
        meta["t"] = time.time()
        self.last_status = meta
        return meta

    def send_waypoint(self, x: float, y: float, query: str = "") -> dict:
        """Task the quad to map-frame ``(x, y)``. Returns the acknowledgement with
        ``req_id`` (the later ``arrived`` event carries it); raises when refused."""
        qx, qy = self.frame.map_to_quad(x, y)
        msg, meta, _ = self._request(
            self.cmd_uri,
            lambda reply_to, rid: protocol.task_request(qx, qy, reply_to, rid),
            "quad_api",
        )
        meta = self._with_map_position(meta)
        if meta.get("accepted") is False:
            self.last_error = f"the quad refused the task: {meta.get('reason') or meta.get('state') or ''}".strip()
            raise QuadUnavailable(self.last_error)
        self.last_status = {**meta, "t": time.time()}
        self.last_command = {"map": [x, y], "quad": [qx, qy], "req_id": msg["req_id"], "query": query,
                             "t": time.time()}  # fmt: skip
        return {"ok": True, "req_id": msg["req_id"], "sent": msg, "ack": meta}

    def capture(self, require_gps: bool = False) -> tuple[dict, bytes]:
        """One image now: (header, jpeg). The header is Penn's meta plus
        ``pose_map`` {x, y, z, yaw} (GPS → map; z = height above takeoff; yaw from
        the unreliable flight-controller heading), ``K``, ``width``, ``height``."""
        _, meta, jpeg = self._request(
            self.image_uri,
            lambda reply_to, rid: protocol.capture_request(reply_to, rid, require_gps),
            "camera_geo_api",
        )
        if jpeg is None:
            raise QuadUnavailable("camera_geo_api replied without an image")
        header = dict(meta)
        header["t"] = time.time()
        w, h = jpeg_size(jpeg)
        header["width"], header["height"] = w, h
        header["K"] = camera.intrinsics(w, h, self.hfov_deg)
        header["stamp_ns"] = (meta.get("image") or {}).get("stamp_ns") or meta.get(
            "time_ns"
        )
        header["pose_map"] = self._capture_pose(meta)
        self.last_image = header
        return header, jpeg

    request_image = capture  # mission.live polls this

    def _capture_pose(self, meta: dict) -> dict | None:
        z = meta.get("rel_alt_m")
        z = float(z) if z is not None and float(z) > 1.0 else self.altitude_m
        yaw = self.frame.yaw_quad_to_map(
            protocol.heading_to_enu_yaw(meta.get("heading_deg"))
        )
        g = protocol.gps(meta)
        xy = (
            self.frame.lonlat_to_map(float(g["longitude"]), float(g["latitude"]))
            if g
            else None
        )
        via = "gps"
        if xy is None and self.last_status and self.last_status.get("position_map"):
            p = self.last_status["position_map"]
            xy, via = (p["x"], p["y"]), "quad_status"
        if xy is None:
            return None
        return {"x": float(xy[0]), "y": float(xy[1]), "z": z, "yaw": yaw, "via": via,
                "heading_reliable": False}  # fmt: skip

    # ---------------------------------------------------------- calibration
    def calibrate(self, samples: int = 3) -> dict:
        """Takeoff point from GPS: UTM(capture GPS) − quad position, from status
        reads taken around the capture. Sets the frame origin."""
        if self.frame.anchor is None:
            raise QuadUnavailable(
                "no geo anchor for this map: cannot place the quad's takeoff point"
            )
        origins, rejected = [], []
        for _ in range(samples):
            s1 = self.status()
            header, _ = self.capture(require_gps=True)
            s2 = self.status()
            p1, p2 = protocol.position(s1), protocol.position(s2)
            g = protocol.gps(header)
            age = (header.get("image") or {}).get("age_s")
            cov = (g or {}).get("position_covariance")
            if p1 is None or p2 is None or g is None:
                rejected.append("no position or GPS")
                continue
            if math.dist(p1, p2) > MAX_CALIBRATION_MOVE_M:
                rejected.append(
                    f"moving ({math.dist(p1, p2):.1f} m during the capture)"
                )
                continue
            if age is not None and float(age) > MAX_IMAGE_AGE_S:
                rejected.append(f"stale image ({float(age):.1f} s)")
                continue
            if cov is not None and float(cov) > MAX_GPS_COVARIANCE:
                rejected.append(f"poor GPS (covariance {float(cov):.1f} m²)")
                continue
            e, n = self.frame.lonlat_to_utm(float(g["longitude"]), float(g["latitude"]))
            px, py = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
            origins.append((e - px, n - py))
        if not origins:
            raise QuadUnavailable("calibration failed: " + "; ".join(rejected))
        e = sum(o[0] for o in origins) / len(origins)
        n = sum(o[1] for o in origins) / len(origins)
        spread = max(math.dist(o, (e, n)) for o in origins)
        self.frame.set_origin(e, n, "gps")
        return {"ok": True, "local_utm_origin": [e, n], "samples": len(origins), "rejected": rejected,
                "spread_m": round(spread, 2)}  # fmt: skip

    # ----------------------------------------------------------------- info
    def info(self) -> dict:
        pose = None
        s = self.last_status
        if s and s.get("position_map"):
            pose = {
                **s["position_map"],
                "z": None,
                "t": s.get("t"),
                "via": "quad_status",
            }
        img = self.last_image
        if (
            img
            and img.get("pose_map")
            and (pose is None or (img.get("t") or 0) > (pose.get("t") or 0))
        ):
            pose = {**img["pose_map"], "t": img.get("t")}
        return {
            "cmd_uri": self.cmd_uri,
            "image_uri": self.image_uri,
            "reply_to": self._reply_to,
            "local_utm_origin": list(self.frame.origin),
            "map_offset": list(self.frame.offset),
            "origin_source": self.frame.origin_source,
            "anchored": self.frame.anchor is not None,
            "last_command": self.last_command,
            "last_status": {
                k: s.get(k)
                for k in (
                    "state",
                    "navigator_state",
                    "distance_m",
                    "waiting_arrival",
                    "target_map",
                    "t",
                )
            }
            if s
            else None,  # fmt: skip
            "pose": pose,
            "error": self.last_error,
        }
