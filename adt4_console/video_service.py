"""Live camera video relay (API.md §12).

Browsers start a stream with ``POST /api/robots/{r}/video/{camera}/start``
and repeat ``.../keepalive`` every 2 s while they show it.  The console keeps
the robot side alive by itself: every ``robot_keepalive_s`` (2 s) it re-sends
the fleetlink C0 ``video`` op while at least one viewer kept the stream alive
within ``viewer_timeout_s`` (6 s), and stops the robot stream otherwise.

Frames from the receiver (``on_update(robot, "video", frame)``) are published
on the hub topic ``video/<robot>/<camera>`` with :meth:`Hub.publish_video`
(GOP cache + per-client drop-until-key-frame).  A frame that follows lost
frames (``gap``) is withheld until the next key frame, so the relay never
forwards a broken delta chain.

The ``mosaic`` stream (the Camera tab's "All cameras" video) is one robot
stream that tiles ``cameras`` into one picture; its frames carry ``tiles``
(each camera's rectangle) and are relayed like any other.

Grace (``grace_s``, 10 s): a browser that unmounts a view (side-tab switch,
robot / camera focus change, drive layout toggle, hidden tab) sends ``stop``
with ``grace: true``.  When that leaves no viewer the stream *lingers*: the
robot keepalive goes on, and a ``start`` / ``keepalive`` within the grace period
picks the running stream up again (same parameters → nothing is sent to the
robot) and the hub replays its GOP cache, so the remounted view shows a
picture at once instead of restarting the robot encoder and waiting for its
next key frame.  A plain ``stop`` (no grace) still stops at once.

Key frames: fleetlink's ``video`` op has no key-frame-interval parameter (the
robot encodes with a fixed GOP of 2 s, ``-g 2*fps``), so the relay asks for a
key frame (C0 ``video_keyframe``, rate-limited to 1/s per stream by the
receiver) whenever it would otherwise make a viewer wait for the next GOP: a
stream picked up again without a cached GOP, and frames withheld after a gap.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

PARAM_KEYS = ("fps", "kbps", "width", "codec", "cameras")
MOSAIC = "mosaic"  # fleetlink pseudo camera: several cameras tiled in one stream


class RateLimited(Exception):
    pass


class _Stream:
    def __init__(self, robot: str, camera: str, params: dict, now: float):
        self.robot = robot
        self.camera = camera
        self.params = dict(params)
        self.viewers: dict[str, float] = {}
        self.t_change = now
        self.t_robot_ka = now
        self.started = now
        self.waiting_key = True
        self.relayed = 0
        self.withheld = 0
        self.last: dict = {}
        self.linger_until = 0.0  # no viewers, kept alive until then (grace)

    @property
    def topic(self) -> str:
        return f"video/{self.robot}/{self.camera}"


class VideoRelay:
    def __init__(
        self,
        live,
        hub,
        clock=time.monotonic,
        viewer_timeout_s: float = 6.0,
        robot_keepalive_s: float = 2.0,
        min_change_s: float = 1.0,
        tick_s: float = 0.5,
        grace_s: float = 10.0,
    ):
        self.live = live  # LiveService (its .receiver may connect later)
        self.hub = hub
        if hub is not None:
            hub.on_video_starved = self._on_starved
        self.clock = clock
        self.viewer_timeout_s = viewer_timeout_s
        self.robot_keepalive_s = robot_keepalive_s
        self.min_change_s = min_change_s
        self.tick_s = tick_s
        self.grace_s = grace_s
        self._streams: dict[tuple[str, str], _Stream] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="video-keepalive", daemon=True
            )
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.tick_s):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("video keepalive tick failed")

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            streams = list(self._streams.values())
        for st in streams:
            self._stop_stream(st)

    # -- robot side --------------------------------------------------------

    def _send(self, st: _Stream, enable: bool, retry: bool) -> None:
        rx = self.live.receiver
        if rx is None:
            return
        try:
            if enable:
                rx.video(st.robot, st.camera, True, retry, **st.params)
            else:
                rx.video(st.robot, st.camera, False, True)
        except Exception:  # noqa: BLE001
            log.exception("video request %s/%s failed", st.robot, st.camera)

    def _keyframe(self, st: _Stream) -> None:
        rx = self.live.receiver
        fn = getattr(rx, "request_keyframe", None) if rx is not None else None
        if fn is None:
            return
        try:
            fn(st.robot, st.camera)  # rate-limited to 1/s per stream there
        except Exception:  # noqa: BLE001
            log.exception("key frame request %s/%s failed", st.robot, st.camera)

    def _stop_stream(self, st: _Stream) -> None:
        with self._lock:
            if self._streams.get((st.robot, st.camera)) is st:
                del self._streams[(st.robot, st.camera)]
        self._send(st, False, True)
        if self.hub is not None:
            self.hub.clear_video(st.topic)

    # -- browser API -------------------------------------------------------

    def start(self, robot: str, camera: str, params: dict, viewer: str = "") -> dict:
        """Start (or retune) a stream.  Raises :class:`RateLimited` when the
        parameters change again within ``min_change_s``."""
        now = self.clock()
        params = {k: params[k] for k in PARAM_KEYS if params.get(k) is not None}
        if "cameras" in params:
            params["cameras"] = list(params["cameras"])
        with self._lock:
            st = self._streams.get((robot, camera))
            if st is None:
                st = self._streams[(robot, camera)] = _Stream(
                    robot, camera, params, now
                )
                changed = True
            else:
                changed = params != st.params
                if changed and now - st.t_change < self.min_change_s:
                    raise RateLimited(
                        f"video parameters changed less than {self.min_change_s:g} s ago"
                    )
            resumed = not st.viewers and st.linger_until > 0
            st.viewers[viewer or "anon"] = now
            st.linger_until = 0.0
            if changed:
                st.params = params
                st.t_change = now
                st.t_robot_ka = now
        if changed:
            self._send(st, True, True)
        elif self.hub is not None and not self.hub.gop(st.topic).get("frames"):
            # a view (re)joins a running stream with nothing cached to replay:
            # ask for a key frame rather than wait up to a GOP for the next one
            self._keyframe(st)
        self._ensure_thread()
        if resumed:
            log.info("video %s/%s picked up again within grace", robot, camera)
        return {"ok": True, "params": dict(st.params), "changed": changed}

    def keepalive(self, robot: str, camera: str, viewer: str = "") -> dict | None:
        """None when the console has no such stream (the browser should start it)."""
        now = self.clock()
        with self._lock:
            st = self._streams.get((robot, camera))
            if st is None:
                return None
            st.viewers[viewer or "anon"] = now
            st.linger_until = 0.0
        return {"ok": True, **self.status(st)}

    def stop(
        self, robot: str, camera: str, viewer: str | None = None, grace: bool = False
    ) -> dict:
        """Remove ``viewer`` (or every viewer).  When none is left the robot
        stream stops at once, or with ``grace`` lingers ``grace_s`` first (a
        ``start`` / ``keepalive`` meanwhile picks it up again)."""
        now = self.clock()
        with self._lock:
            st = self._streams.get((robot, camera))
            if st is None:
                return {"ok": True, "active": False}
            if viewer:
                st.viewers.pop(viewer, None)
            else:
                st.viewers.clear()
            remaining = len(st.viewers)
            linger = not remaining and grace and self.grace_s > 0
            if linger:
                st.linger_until = max(st.linger_until, now + self.grace_s)
        if linger:
            return {"ok": True, "active": True, "viewers": 0, "grace_s": self.grace_s}
        if not remaining:
            self._stop_stream(st)
        return {"ok": True, "active": bool(remaining)}

    def tick(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        due, dead = [], []
        with self._lock:
            for st in self._streams.values():
                for v, t in list(st.viewers.items()):
                    if now - t > self.viewer_timeout_s:
                        del st.viewers[v]
                if not st.viewers and now >= st.linger_until:
                    dead.append(st)
                elif now - st.t_robot_ka >= self.robot_keepalive_s:
                    st.t_robot_ka = now
                    due.append(st)
        for st in dead:
            self._stop_stream(st)
        for st in due:
            self._send(st, True, False)

    # -- frames ------------------------------------------------------------

    def on_frame(self, robot: str, frame: dict) -> None:
        """Receiver callback (receiver thread)."""
        camera = frame.get("camera")
        key = bool(frame.get("key"))
        withheld = False
        with self._lock:
            st = self._streams.get((robot, camera))
            if st is not None:
                if frame.get("gap") and not key:
                    st.waiting_key = True
                if st.waiting_key and not key:
                    st.withheld += 1
                    withheld = True
                else:
                    st.waiting_key = False
                    st.relayed += 1
                    st.last = {k: frame.get(k) for k in ("codec", "width", "height", "seq")}
        if withheld:
            # don't freeze every viewer until the next GOP boundary (up to 2 s)
            self._keyframe(st)
            return
        if st is None:
            return  # nobody asked for it (e.g. a stream still winding down)
        if self.hub is not None:
            data = {k: v for k, v in frame.items() if k != "gap"}
            data["robot"] = robot
            self.hub.publish_video(f"video/{robot}/{camera}", data, key)

    # -- status ------------------------------------------------------------

    def _on_starved(self, topic: str) -> None:
        """Hub callback: a viewer started dropping until the next key frame."""
        parts = topic.split("/")  # video/<robot>/<camera>
        rx = self.live.receiver
        if len(parts) == 3 and rx is not None and hasattr(rx, "request_keyframe"):
            rx.request_keyframe(parts[1], parts[2])  # rate-limited to 1/s there

    def status(self, st: _Stream) -> dict:
        now = self.clock()
        rx_stats = {}
        rx = self.live.receiver
        fn = getattr(rx, "video_stats", None) if rx is not None else None
        if fn is not None:
            try:
                rx_stats = (fn(st.robot) or {}).get(st.camera) or {}
            except Exception:  # noqa: BLE001
                rx_stats = {}
        rx_stats = dict(rx_stats)
        robot_side = rx_stats.pop("robot", None)
        return {
            "camera": st.camera,
            "active": True,
            "params": dict(st.params),
            "viewers": len(st.viewers),
            "lingering_s": max(0.0, st.linger_until - now) if not st.viewers else 0.0,
            "age_s": now - st.started,
            "relay": {
                "frames": st.relayed,
                "withheld": st.withheld,
                "waiting_key": st.waiting_key,
                "gop": self.hub.gop(st.topic) if self.hub is not None else {},
                **st.last,
            },
            "rx": rx_stats,
            "robot": robot_side,
        }

    def streams(self, robot: str) -> dict:
        """Streams relayed here, plus any other stream the robot reports or we
        still receive (``active: false`` = not relayed by this console)."""
        with self._lock:
            mine = [st for (r, _), st in self._streams.items() if r == robot]
        out = {st.camera: self.status(st) for st in mine}
        rx = self.live.receiver
        fn = getattr(rx, "video_stats", None) if rx is not None else None
        try:
            others = (fn(robot) or {}) if fn is not None else {}
        except Exception:  # noqa: BLE001
            others = {}
        for cam, stats in others.items():
            if cam not in out and stats.get("robot"):
                stats = dict(stats)
                out[cam] = {"camera": cam, "active": False,
                            "robot": stats.pop("robot", None), "rx": stats}  # fmt: skip
        return out
