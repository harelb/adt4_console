"""Console side of fleetlink: owns the ``fleetlink.receiver.Receiver`` (API.md §8).

The receiver is imported lazily; with ``--no-fleetlink`` (or no zenoh /
fleetlink) ``receiver`` stays ``None`` and live endpoints return empty.

``ZenohTransport`` never fails for want of a router: it retries the open in
the background (and reconnects after router restarts), so the receiver is
created at once and :attr:`LiveService.status` reports ``waiting`` until the
session is open, then ``connected``.

Live image-store mirror (fleetlink class C6, API.md §13): the receiver writes
the robots' store frames into ``<live_store_root>/<robot>/<store_id>/image_store``,
a valid hydra image store that SAM3 search can load while the run goes on. The
default root is the CURRENT run's output directory: ``$ADT4_OUTPUT_DIR/live``
(run-adt4 / start-adt4-console set it), else ``<output_root>/console_<start
time>/live``; never a folder shared across runs, whose older mirrors are in other
runs' odometry frames. An empty ``live_store_root`` disables mirroring.

Live state for the SAM3 query server (:meth:`LiveService.write_live_state`): every
``LIVE_STATE_PERIOD_S`` the console writes ``live_state_path`` (default
``<output_root>/console_live.json``, a fixed place because openset_query runs in
another session with another ADT4_OUTPUT_DIR) with the mirror root, each robot's
current store and whether it is relocalized, with its ``map <- robot frame``
alignment. openset_query searches only those stores, posed into the prior map, and
only while the robot is relocalized.

Lidar (fleetlink C3 ``lidar/points``, off by default): :meth:`LiveService.set_lidar`
asks a robot to stream it, :meth:`LiveService.lidar` is the latest frame.  Each
read (the console polls ``GET .../lidar`` ~1 Hz while the toggle is on) is the
keepalive: unread for ``LIDAR_IDLE_S`` the receiver sends ``enabled: false``
itself, so a closed page does not leave the robot streaming.  The agent also
stops on its own when the base's refreshes stop (``lidar_ttl_s``, 60 s).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

from .video_service import VideoRelay

log = logging.getLogger(__name__)
LIDAR_IDLE_S = 15.0  # no lidar GET for this long after an enable: turn it off
LIVE_STATE_PERIOD_S = 2.0  # console_live.json refresh (openset_query: stale after 30 s)
LIVE_STATE_VERSION = 1


class LiveService:
    def __init__(self, settings, hub=None, receiver=None):
        self.settings = settings
        self.hub = hub
        self.receiver = receiver
        self.graph = None  # GraphService, wired by the app
        self.error: str | None = None  # the receiver could not be created at all
        self.camera_timeout_s = 5.0
        self._stop = threading.Event()
        self.video = VideoRelay(self, hub)  # live camera video (API.md §12)
        self.started_ns = time.time_ns()
        self._started_tag = time.strftime("%Y%m%d_%H%M%S")
        self._state_thread = None

    def start(self) -> None:
        if self.receiver is not None:
            self.receiver.start()
        elif self.settings.fleetlink:
            self._connect()
        if self.live_state_path and self.store_root:
            self._state_thread = threading.Thread(
                target=self._state_loop, name="live-state", daemon=True
            )
            self._state_thread.start()

    def _connect(self) -> bool:
        try:
            from fleetlink.receiver import Receiver
            from fleetlink.transport import ZenohTransport

            # opens at once or keeps retrying in the background (zenohd usually
            # comes up after the console)
            transport = ZenohTransport(
                connect=self.settings.zenoh_connect or "tcp/127.0.0.1:7447"
            )
            receiver = Receiver(
                transport,
                self.on_update,
                store_root=self.store_root,
                lidar_idle_s=LIDAR_IDLE_S,
            )
            receiver.start()
        except Exception as e:  # noqa: BLE001  (e.g. zenoh / fleetlink missing)
            self.error = f"{type(e).__name__}: {e}"
            log.warning("fleetlink unavailable: %s", self.error)
            return False
        self.receiver = receiver
        self.error = None
        opened = getattr(transport, "opened", None)
        if opened is not None and not opened.is_set():
            log.info(
                "fleetlink waiting for a zenoh router at %s",
                ", ".join(getattr(transport, "endpoints", []) or []),
            )
            threading.Thread(
                target=self._log_when_open,
                args=(opened,),
                name="fleetlink-open-wait",
                daemon=True,
            ).start()
        else:
            log.info("fleetlink receiver connected")
        return True

    def _log_when_open(self, opened: threading.Event) -> None:
        while not self._stop.is_set():
            if opened.wait(1.0):
                log.info("fleetlink receiver connected")
                return

    @property
    def store_root(self) -> str | None:
        """Where live store mirrors go (None = mirroring disabled): the current run's
        output directory, never a folder shared with earlier runs."""
        root = getattr(self.settings, "live_store_root", None)
        if root is None:
            run_dir = os.environ.get("ADT4_OUTPUT_DIR")
            if not run_dir:
                out = getattr(self.settings, "output_root", None) or "~/adt4_output"
                run_dir = os.path.join(out, f"console_{self._started_tag}")
            root = os.path.join(run_dir, "live")
        return os.path.expanduser(root) if root else None

    @property
    def live_state_path(self) -> str | None:
        """Where the live state for the SAM3 query server goes (None = off)."""
        path = getattr(self.settings, "live_state_path", None)
        if path is None:
            out = getattr(self.settings, "output_root", None) or "~/adt4_output"
            path = os.path.join(out, "console_live.json")
        return os.path.expanduser(path) if path else None

    def live_state(self) -> dict:
        """The current run's mirrors and each robot's relocalization (see module doc)."""
        from .routes.change_detection import relocalized

        stores = (self.store_stats() or {}).get("robots") or {}
        robots = {}
        for robot in sorted(set(stores) | set(self.robots())):
            st = self.state(robot)
            fa = (st or {}).get("frame_align")
            if fa is None and self.receiver is not None:
                try:
                    fa = (self.receiver.link(robot) or {}).get("frame_align")
                except Exception:  # noqa: BLE001
                    fa = None
            rel = relocalized(st, robot)
            mirror = stores.get(robot) or {}
            robots[robot] = {
                "store_id": mirror.get("store_id"),
                "path": mirror.get("path"),
                "relocalized": bool(rel.get("ok")),
                "reason": rel.get("reason"),
                "frame_align": (
                    {k: fa.get(k) for k in ("ok", "parent", "child", "T")}
                    if isinstance(fa, dict)
                    else None
                ),
            }
        return {
            "version": LIVE_STATE_VERSION,
            "written_ns": time.time_ns(),
            "console_started_ns": self.started_ns,
            "pid": os.getpid(),
            "live_store_root": self.store_root,
            "robots": robots,
        }

    def write_live_state(self) -> str | None:
        """Write :meth:`live_state` atomically to :attr:`live_state_path`."""
        path = self.live_state_path
        if not path:
            return None
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(self.live_state(), f)
        os.replace(tmp, path)
        return path

    def _state_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.write_live_state()
            except Exception:  # noqa: BLE001
                log.exception("writing the live state")
            self._stop.wait(LIVE_STATE_PERIOD_S)
        self._remove_live_state()

    def _remove_live_state(self) -> None:
        """On a clean stop, drop our state file (not another console's)."""
        path = self.live_state_path
        try:
            with open(path) as f:
                if json.load(f).get("pid") != os.getpid():
                    return
            os.remove(path)
        except (OSError, ValueError, TypeError):
            pass

    def store_stats(self) -> dict:
        """``{root, robots: {robot: mirror stats}}`` (robots empty when off)."""
        fn = getattr(self.receiver, "store_stats", None)
        robots = {}
        if fn is not None:
            try:
                robots = fn() or {}
            except Exception:  # noqa: BLE001
                robots = {}
        enabled = getattr(self.receiver, "store", None) is not None
        return {"root": self.store_root if enabled else None, "robots": robots}

    @property
    def status(self) -> dict:
        """``{state, endpoints, error}``; state is ``disabled`` (--no-fleetlink),
        ``error`` (no receiver: ``error`` says why), ``waiting`` (zenoh session
        not open yet: no router reachable) or ``connected``."""
        if self.receiver is None:
            if self.error is not None:
                return {"state": "error", "endpoints": [], "error": self.error}
            return {"state": "disabled", "endpoints": [], "error": None}
        tr = self.receiver.transport
        endpoints = list(getattr(tr, "endpoints", []) or [])
        opened = getattr(tr, "opened", None)
        if opened is not None and not opened.is_set():
            where = ", ".join(endpoints) or "?"
            err = getattr(tr, "open_error", None)
            msg = f"waiting for a zenoh router at {where}"
            return {
                "state": "waiting",
                "endpoints": endpoints,
                "error": f"{msg} ({err})" if err else msg,
            }
        return {"state": "connected", "endpoints": endpoints, "error": None}

    def stop(self) -> None:
        self._stop.set()
        try:
            self.video.close()
        except Exception:  # noqa: BLE001
            pass
        if self.receiver is not None:
            try:
                self.receiver.stop()
                close = getattr(self.receiver.transport, "close", None)
                if close:
                    close()
            except Exception:  # noqa: BLE001
                pass

    @property
    def available(self) -> bool:
        """The receiver exists (live endpoints work, possibly still empty)."""
        return self.receiver is not None

    @property
    def connected(self) -> bool:
        return self.status["state"] == "connected"

    def robots(self) -> list[str]:
        return list(self.receiver.robots()) if self.receiver is not None else []

    def on_update(self, robot: str, kind: str, data) -> None:
        """Receiver callback (receiver thread)."""
        try:
            if kind == "video":
                self.video.on_frame(robot, data)
            elif kind in ("snapshot", "delta", "tiles"):
                if self.graph is not None:
                    self.graph.on_live(robot, kind, data)
                if kind == "snapshot":
                    self._clear_stale_overlays(robot)
            elif kind == "state" and self.hub is not None:
                self.hub.publish(f"robot/{robot}/state", data)
            elif kind == "link" and self.hub is not None:
                self.hub.publish(f"link/{robot}", data)
            elif kind == "grid" and self.hub is not None:
                self.hub.publish(f"grid/{robot}", data)
            elif kind == "paths" and self.hub is not None:
                self.hub.publish(f"robot/{robot}/paths", data)
        except Exception:  # noqa: BLE001
            log.exception("live update %s/%s failed", robot, kind)

    def _clear_stale_overlays(self, robot: str) -> None:
        """After a (re)sync — e.g. the robot's agent restarted with a new epoch —
        the hub still caches the previous incarnation's latest paths/grid and
        would replay them to every new browser; overwrite them with "none"."""
        if self.hub is None or self.receiver is None:
            return
        if self.receiver.paths(robot) is None:
            self.hub.publish(f"robot/{robot}/paths", None)
        if self.receiver.grid(robot) is None:
            self.hub.publish(f"grid/{robot}", {"hash": None})

    def links(self) -> dict:
        if self.receiver is None:
            return {
                "robots": {},
                "transport": self.status,
                "store": {"root": None, "robots": {}},
            }
        return {
            "robots": {r: self.receiver.link(r) for r in self.receiver.robots()},
            "transport": self.status,
            "store": self.store_stats(),
        }

    def state(self, robot: str) -> dict | None:
        if self.receiver is None:
            return None
        try:
            st = self.receiver.state(robot)
        except Exception:  # noqa: BLE001
            return None
        if st is None:
            return None
        st = dict(st)
        st["paths"] = self.paths(robot)
        return st

    def _call(self, name: str, robot: str):
        fn = getattr(self.receiver, name, None) if self.receiver is not None else None
        if fn is None:
            return None
        try:
            return fn(robot)
        except Exception:  # noqa: BLE001
            return None

    def grid(self, robot: str) -> dict | None:
        """Latest occupancy grid wire dict (``data`` = zlib int8 bytes)."""
        return self._call("grid", robot)

    def paths(self, robot: str) -> dict | None:
        return self._call("paths", robot)

    def lidar(self, robot: str) -> dict | None:
        """Latest lidar frame (``Receiver.lidar``), or None."""
        return self._call("lidar", robot)

    def set_lidar(self, robot: str, enabled: bool) -> dict:
        """Turn the robot's lidar stream on / off; ``{request, status}``."""
        req = self.receiver.set_lidar(robot, bool(enabled))
        return {"request": req, "status": self.receiver.lidar_status(robot)}

    def cameras(self, robot: str) -> list[str]:
        return list(self._call("cameras", robot) or [])

    def camera(self, robot: str, name: str, width: int, quality: int) -> dict | None:
        """Blocking C4 request; None on timeout."""
        return self.receiver.camera(
            robot, name, width=width, quality=quality, timeout=self.camera_timeout_s
        )
