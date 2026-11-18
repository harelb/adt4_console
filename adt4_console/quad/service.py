"""Penn quad service: goals, captures and SAM3 on captures (API.md §16).

Owns the :class:`~.link.QuadLink`. A poller reads the quad's status every
``quad_poll_s`` (published as ``quad/state`` for the map), calibrates the
takeoff point from GPS until it has one, and watches the active goal: the
quad's ``arrived`` event ends it, or (fallback) the quad within
``quad_arrive_m`` of it; ``quad_goal_timeout_s`` fails it.

On arrival a goal with ``capture`` takes an image (and runs SAM3 with the
goal's ``query``), then ``quad_event`` {kind: "captured"} is published: the
chat shows the image and starts a follow-up turn that attaches it.
"""

from __future__ import annotations

import base64
import collections
import io
import itertools
import logging
import math
import os
import pathlib
import threading
import time

from . import camera, protocol
from .link import QuadLink, QuadUnavailable

log = logging.getLogger(__name__)

MAX_CAPTURES = 200
MODEL_IMAGE_PX = 1024  # longest side of an image sent to the language model
CALIBRATE_EVERY_S = 10.0


def downscale_jpeg(
    jpeg: bytes, max_px: int = MODEL_IMAGE_PX, quality: int = 80
) -> bytes:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(jpeg)) as im:
            if max(im.size) <= max_px:
                return jpeg
            im = im.convert("RGB")
            im.thumbnail((max_px, max_px))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=quality)
            return out.getvalue()
    except Exception:  # noqa: BLE001 — the original is still a valid image
        return jpeg


def data_uri(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()


class QuadService:
    def __init__(self, link: QuadLink, hub=None, settings=None):
        self.link = link
        self.hub = hub
        self.settings = settings
        self.poll_s = float(getattr(settings, "quad_poll_s", 1.0) or 1.0)
        self.arrive_m = float(getattr(settings, "quad_arrive_m", 3.0))
        self.goal_timeout_s = float(getattr(settings, "quad_goal_timeout_s", 600.0))
        d = getattr(settings, "quad_images_dir", None)
        self.images_dir = (
            pathlib.Path(os.path.expanduser(d)) / "captures" if d else None
        )
        self.captures: collections.OrderedDict[str, dict] = collections.OrderedDict()
        self.goals: dict[str, dict] = {}
        self.active: str | None = None  # goal id
        self._ids = itertools.count(1)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_calibration = 0.0
        self.calibration: dict | None = None
        link.on_event(self._on_link_event)

    # ------------------------------------------------------------ lifecycle
    @property
    def configured(self) -> bool:
        return self.link.configured

    def start(self) -> None:
        if not self.configured or (
            self._thread is not None and self._thread.is_alive()
        ):
            return
        self.link.start()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._poll, name="quad-poll", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None
        self.link.stop()

    def _publish(self, topic: str, data) -> None:
        if self.hub is not None:
            try:
                self.hub.publish(topic, data)
            except Exception:  # noqa: BLE001
                log.exception("quad publish %s", topic)

    def _event(self, kind: str, **data) -> dict:
        ev = {"kind": kind, "t": time.time(), **data}
        self._publish("quad_event", ev)
        return ev

    # ------------------------------------------------------------- polling
    def _poll(self) -> None:
        while not self._stop.wait(self.poll_s):
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001
                log.exception("quad poll")

    def poll_once(self) -> None:
        status = None
        if self.link.cmd_uri:
            try:
                status = self.link.status()
            except QuadUnavailable as e:
                log.debug("quad status: %s", e)
        self._maybe_calibrate()
        self._check_goal(status)
        self._publish("quad/state", self.state())

    def _maybe_calibrate(self) -> None:
        f = self.link.frame
        if f.origin_source is not None or f.anchor is None:
            return
        takeoff = getattr(self.settings, "quad_takeoff", None)
        if takeoff:  # --quad-takeoff LAT LON wins over GPS calibration
            e, n = f.lonlat_to_utm(float(takeoff[1]), float(takeoff[0]))
            f.set_origin(e, n, "takeoff_flag")
            self.calibration = {
                "ok": True,
                "local_utm_origin": [e, n],
                "source": "takeoff_flag",
            }
            return
        if not (self.link.cmd_uri and self.link.image_uri):
            return
        if time.time() - self._last_calibration < CALIBRATE_EVERY_S:
            return
        self._last_calibration = time.time()
        try:
            self.calibration = self.link.calibrate()
            log.info("quad takeoff calibrated from GPS: %s", self.calibration)
            self._event("calibrated", **self.calibration)
        except QuadUnavailable as e:
            self.calibration = {"ok": False, "error": str(e)}
            log.info("quad calibration: %s", e)

    def calibrate(self) -> dict:
        self.calibration = self.link.calibrate()
        self._event("calibrated", **self.calibration)
        return self.calibration

    def state(self) -> dict:
        info = self.link.info()
        with self._lock:
            g = self.goals.get(self.active) if self.active else None
            last = next(reversed(self.captures)) if self.captures else None
        return {"configured": self.configured, **info, "goal": _public_goal(g), "last_capture": last,
                "calibration": self.calibration}  # fmt: skip

    # --------------------------------------------------------------- goals
    def goal(self, x: float, y: float, capture: bool = True, query: str = "", label: str = "",
             chat: dict | None = None) -> dict:  # fmt: skip
        """Fly to map ``(x, y)``; on arrival capture (and segment ``query``)."""
        ack = self.link.send_waypoint(x, y, query)
        gid = f"g{next(self._ids)}"
        g = {"id": gid, "req_id": ack["req_id"], "x": float(x), "y": float(y), "capture": bool(capture),
             "query": (query or "").strip(), "label": label, "chat": chat or {}, "status": "flying",
             "t0": time.time(), "capture_id": None, "error": None}  # fmt: skip
        with self._lock:
            prev = self.goals.get(self.active) if self.active else None
            if prev is not None and prev["status"] == "flying":
                prev["status"] = "preempted"
                self._event("preempted", goal=_public_goal(prev))
            self.goals[gid] = g
            self.active = gid
        self._event("sent", goal=_public_goal(g))
        return {"ok": True, "goal": _public_goal(g), "ack": ack["ack"]}

    def _on_link_event(self, meta: dict, jpeg) -> None:
        if not protocol.is_arrival(meta):
            return
        rid = str(meta.get("req_id", ""))
        with self._lock:
            g = next((g for g in self.goals.values() if g["req_id"] == rid), None)
        if g is not None:
            self._arrive(g, "arrived event")

    def _check_goal(self, status: dict | None) -> None:
        with self._lock:
            g = self.goals.get(self.active) if self.active else None
        if g is None or g["status"] != "flying":
            return
        pm = (status or {}).get("position_map")
        if (
            pm is not None
            and math.dist((pm["x"], pm["y"]), (g["x"], g["y"])) <= self.arrive_m
        ):
            self._arrive(g, f"within {self.arrive_m:g} m")
        elif time.time() - g["t0"] > self.goal_timeout_s:
            with self._lock:
                g["status"], g["error"] = (
                    "failed",
                    f"not there after {self.goal_timeout_s:.0f} s",
                )
            self._event("failed", goal=_public_goal(g))

    def _arrive(self, g: dict, how: str) -> None:
        with self._lock:
            if g["status"] != "flying":
                return
            g["status"], g["arrived_via"], g["t_arrived"] = "arrived", how, time.time()
        self._event("arrived", goal=_public_goal(g))
        if g["capture"]:
            # never on the link's receiver thread: a capture waits for that thread
            threading.Thread(
                target=self._capture_for_goal,
                args=(g,),
                name="quad-capture",
                daemon=True,
            ).start()

    def _capture_for_goal(self, g: dict) -> None:
        time.sleep(1.0)  # let the quad settle at the point
        try:
            c = self.capture(query=g["query"], goal_id=g["id"])
        except Exception as e:  # noqa: BLE001
            with self._lock:
                g["error"] = f"capture failed: {e}"
            self._event("capture_failed", goal=_public_goal(g), error=str(e))
            return
        with self._lock:
            g["capture_id"] = c["id"]
        self._event("captured", goal=_public_goal(g), capture=self.public_capture(c))

    # ------------------------------------------------------------ captures
    def capture(self, query: str = "", goal_id: str | None = None) -> dict:
        header, jpeg = self.link.capture()
        cid = f"q{next(self._ids)}"
        c = {"id": cid, "t": time.time(), "header": header, "jpeg": jpeg, "goal_id": goal_id,
             "segment": None, "overlay": None}  # fmt: skip
        if self.images_dir is not None:
            try:
                self.images_dir.mkdir(parents=True, exist_ok=True)
                path = self.images_dir / f"{time.strftime('%Y%m%d_%H%M%S')}_{cid}.jpg"
                path.write_bytes(jpeg)
                c["path"] = str(path)
            except OSError as e:
                log.warning("quad capture not saved: %s", e)
        with self._lock:
            self.captures[cid] = c
            while len(self.captures) > MAX_CAPTURES:
                self.captures.popitem(last=False)
        if query:
            try:
                self.segment(cid, query)
            except Exception as e:  # noqa: BLE001 — the image is still useful
                c["segment"] = {"query": query, "error": str(e)}
        return c

    def get(self, cid: str) -> dict | None:
        with self._lock:
            return self.captures.get(cid)

    def segment(self, cid: str, query: str, conf: float | None = None) -> dict:
        from .. import sam3_client

        c = self.get(cid)
        if c is None:
            raise KeyError(f"no quad capture {cid}")
        url = getattr(self.settings, "sam3_url", None) or "http://127.0.0.1:8077"
        out = sam3_client.segment(c["jpeg"], query, url, conf=conf)
        pose, K = c["header"].get("pose_map"), c["header"].get("K")
        dets = []
        for d in out.get("detections") or []:
            det = {
                k: d.get(k)
                for k in ("label", "score", "box_xyxy", "center_px", "area_px")
            }
            if pose and K and d.get("center_px"):
                gx, gy = camera.pixel_to_ground(
                    d["center_px"][0], d["center_px"][1], pose, K
                )
                det["map_xy_approx"] = [round(gx, 1), round(gy, 1)]
            dets.append(det)
        overlay = out.get("overlay_jpeg_b64")
        c["overlay"] = base64.b64decode(overlay) if overlay else None
        c["segment"] = {
            "query": query,
            "detections": dets,
            "elapsed_s": out.get("elapsed_s"),
        }
        return c["segment"]

    def image(self, cid: str, overlay: bool = False) -> bytes | None:
        c = self.get(cid)
        if c is None:
            return None
        return (c.get("overlay") if overlay else None) or c["jpeg"]

    def public_capture(self, c: dict) -> dict:
        h = c["header"]
        return {"id": c["id"], "t": c["t"], "goal_id": c.get("goal_id"),
                "image_url": f"api/quad/captures/{c['id']}.jpg",
                "overlay_url": f"api/quad/captures/{c['id']}/overlay.jpg" if c.get("overlay") else None,
                "pose_map": h.get("pose_map"), "gps": h.get("gps"), "rel_alt_m": h.get("rel_alt_m"),
                "heading_deg": h.get("heading_deg"), "width": h.get("width"), "height": h.get("height"),
                "stamp_utc": (h.get("image") or {}).get("stamp_utc"), "segment": c.get("segment")}  # fmt: skip

    def for_model(self, cid: str) -> tuple[dict, list[str]]:
        """(summary for the language model, [image data URIs]): the capture, or its
        SAM3 overlay when there is one."""
        c = self.get(cid)
        if c is None:
            raise KeyError(f"no quad capture {cid}")
        p = self.public_capture(c)
        summary = {"capture_id": cid, "pose_map": p["pose_map"], "rel_alt_m": p["rel_alt_m"],
                   "stamp_utc": p["stamp_utc"], "image": f"{p['width']}x{p['height']} nadir (top = the quad's "
                   "heading, which is unreliable)", "segment": p["segment"]}  # fmt: skip
        img = c.get("overlay") or c["jpeg"]
        return summary, [data_uri(downscale_jpeg(img))]


def _public_goal(g: dict | None) -> dict | None:
    if g is None:
        return None
    return {k: g.get(k) for k in ("id", "x", "y", "capture", "query", "label", "status", "t0", "capture_id",
                                  "error", "arrived_via", "chat")}  # fmt: skip
