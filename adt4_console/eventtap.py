"""Event taps (mission logging plan §3): what the console already sees, as events.

* :class:`HubTap` watches hub publishes (cheap, on the publishing thread; the
  writes are queued by :mod:`eventlog`), throttled per robot: ``link.stats``
  every 5 s, ``clock.offset`` every 30 s, ``robot.pose`` at 1 Hz,
  ``reloc.state`` on change (checked at most 1 Hz), ``awcd.changes`` on change,
  ``mission.ack`` when a robot's fleetlink agent acknowledges a new mission id
  (C1 state ``mission``; its ``run`` is kept for the mission's run ids).
  Video and graph deltas are ignored.
* :class:`RestTap` is an ASGI middleware logging operator REST actions
  (``op.take_lease``, ``op.pause/resume/stop``, ``op.scene_repair``,
  ``op.manip``, ``op.awcd_clear``, ...), plus ``goal.approved`` /
  ``goal.rejected`` around the goal routes (``X-ADT4-Goal-Origin``: who
  approved: auto / operator / plan_panel) and the chat's write decisions.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time

from . import eventlog

log = logging.getLogger(__name__)

SOURCE = "console"
LINK_S = 5.0
CLOCK_S = 30.0
POSE_S = 1.0
RELOC_CHECK_S = 1.0
AWCD_MIN_S = 2.0

LINK_KEYS = ("connected", "staleness_s", "latency_ms", "rtt_ms", "clock_offset_ms", "drops", "gaps", "resyncs",
             "resend_requests", "resend_escalations", "rx_rate_Bps", "tx_rate_Bps", "rx_total_B", "tx_total_B",
             "epoch")  # fmt: skip


def _yaw(p: dict) -> float | None:
    if p.get("yaw") is not None:
        return p.get("yaw")
    q = [p.get(k) for k in ("qw", "qx", "qy", "qz")]
    if any(v is None for v in q):
        return None
    qw, qx, qy, qz = (float(v) for v in q)
    return math.atan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy * qy + qz * qz))


class HubTap:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self._last: dict[tuple, float] = {}
        self._reloc: dict[str, tuple] = {}
        self._awcd: str | None = None
        self.link: dict[str, dict] = {}  # robot -> latest link stats (clock offsets for missions)
        self.runs: dict[str, dict] = {}  # robot -> its fleetlink agent's run (run_id, session_id)
        self._acked: dict[str, tuple] = {}  # robot -> (mission_id, state) it last acknowledged

    def _due(self, key, period) -> bool:
        now = self.clock()
        with self.lock:
            if now - self._last.get(key, -1e18) < period:
                return False
            self._last[key] = now
            return True

    def __call__(self, topic: str, data) -> None:
        if topic.startswith("video/") or topic.endswith("/delta"):
            return
        try:
            if topic.startswith("link/"):
                self._on_link(topic[5:], data)
            elif topic.startswith("robot/") and topic.endswith("/state"):
                self._on_state(topic[6:-6], data)
            elif topic == "awcd/changes":
                self._on_awcd(data)
        except Exception:  # noqa: BLE001 — a tap must never break a publish
            log.exception("hub tap %s", topic)

    def _on_link(self, robot: str, data) -> None:
        if not isinstance(data, dict):
            return
        self.link[robot] = data
        if self._due(("link", robot), LINK_S):
            out = {k: data.get(k) for k in LINK_KEYS if k in data}
            loss = data.get("loss")
            if isinstance(loss, dict):
                out["loss"] = loss
            out["robot"] = robot
            eventlog.emit(SOURCE, "link.stats", out, robot=robot)
        if data.get("clock_offset_ms") is not None and self._due(("clock", robot), CLOCK_S):
            self.clock_offset(robot, data)

    def clock_offset(self, robot: str, data: dict | None = None, why: str = "periodic") -> None:
        data = data or self.link.get(robot) or {}
        eventlog.emit(SOURCE, "clock.offset", {"robot": robot, "offset_ms": data.get("clock_offset_ms"),
                                               "rtt_ms": data.get("rtt_ms"), "why": why}, robot=robot)  # fmt: skip

    def _on_state(self, robot: str, data) -> None:
        if not isinstance(data, dict):
            return
        run = data.get("run")
        if isinstance(run, dict) and run.get("run_id"):
            self.runs[robot] = run
        m = data.get("mission")
        if isinstance(m, dict):
            key = (m.get("mission_id") or "", m.get("state") or "")
            if self._acked.get(robot) != key:
                self._acked[robot] = key
                eventlog.emit(SOURCE, "mission.ack", {"robot": robot, "mission_id": key[0], "state": key[1],
                                                      "session_id": m.get("session_id"), "t_ack_robot": m.get("t_ack"),
                                                      "run_id": (run or {}).get("run_id")}, robot=robot)  # fmt: skip
        p = data.get("pose")
        if isinstance(p, dict) and self._due(("pose", robot), POSE_S):
            out = {k: p.get(k) for k in ("x", "y", "z") if k in p}
            out.update(robot=robot, yaw=_yaw(p), frame=p.get("frame") or p.get("frame_id"))
            eventlog.emit(SOURCE, "robot.pose", out, robot=robot)
        if self._due(("reloc", robot), RELOC_CHECK_S):
            from .routes.change_detection import relocalized

            v = relocalized(data, robot)
            key = (v.get("state"), bool(v.get("ok")), v.get("matches"))
            if self._reloc.get(robot) != key:
                self._reloc[robot] = key
                eventlog.emit(SOURCE, "reloc.state", {"robot": robot, **v}, robot=robot)

    def _on_awcd(self, data) -> None:
        if not isinstance(data, dict):
            return
        summary = {"counts": data.get("counts"), "robots": data.get("robots"), "topic": data.get("topic")}
        key = json.dumps(summary, sort_keys=True, default=str)
        if key == self._awcd:
            return
        if not self._due(("awcd",), AWCD_MIN_S) and self._awcd is not None:
            return
        self._awcd = key
        eventlog.emit(SOURCE, "awcd.changes", {**summary, "n": len(data.get("changes") or [])})


# --------------------------------------------------------------------- REST
# (method, path regex, event type, path-param names); first match wins
RULES = [
    ("POST", r"/api/robots/([^/]+)/take_lease", "op.take_lease", ("robot",)),
    ("POST", r"/api/robots/([^/]+)/executor/(pause|resume|stop)", "op.{1}", ("robot", "cmd")),
    ("POST", r"/api/robots/([^/]+)/teleop/(stop|resume|pause|stand|sit|arm)", "op.teleop", ("robot", "cmd")),
    ("POST", r"/api/robots/([^/]+)/stop_save", "op.stop_save", ("robot",)),
    ("POST", r"/api/robots/([^/]+)/save_dsg", "op.save_dsg", ("robot",)),
    ("POST", r"/api/plan/scene_repair/([^/]+)/(confirm|decline)", "op.scene_repair", ("rid", "decision")),
    ("POST", r"/api/manipulation/requests/([^/]+)/respond", "op.manip", ("request_id",)),
    ("POST", r"/api/change_detection/clear", "op.awcd_clear", ()),
    ("POST", r"/api/robots/([^/]+)/change_detection/([a-z_]+)", "op.change_detection", ("robot", "cmd")),
    ("POST", r"/api/robots/([^/]+)/constraints", "op.constraints_add", ("robot",)),
    ("DELETE", r"/api/robots/([^/]+)/constraints", "op.constraints_clear", ("robot",)),
    ("POST", r"/api/chat/cypher_write", "chat.cypher_write", ()),
    ("POST", r"/api/chat/map_edit", "chat.map_edit", ()),
    ("POST", r"/api/chat/scene_edit", "chat.scene_edit", ()),
    ("POST", r"/api/chat/scene_edit/undo", "chat.scene_edit_undo", ()),
    ("POST", r"/api/fleet/([^/]+)/tmux/([^/]+)/([a-z_]+)", "op.tmux", ("machine", "session", "action")),
    ("POST", r"/api/jobs/([^/]+)/(confirm|cancel)", "op.job", ("job_id", "action")),
    ("DELETE", r"/api/launch/active", "op.launch_clear", ()),
    ("POST", r"/api/missions/([^/]+)/(execute|abort)", "op.mission_plan", ("plan_mission", "action")),
    ("POST", r"/api/logbook/missions/([^/]+)/(collect|export)", "op.mission_{1}", ("mission", "action")),
    ("POST", r"/api/robots/([^/]+)/goal/(language|goto|tsp|pddl)", "goal", ("robot", "kind")),
    ("POST", r"/api/robots/([^/]+)/path/direct", "goal", ("robot",)),
]
_COMPILED = [(m, re.compile(p + r"/?$"), t, names) for m, p, t, names in RULES]
MAX_BODY = 256 * 1024
RESPONSE_KEYS = ("ok", "error", "detail", "message", "track", "via", "subscribers", "delivered", "warning", "symbol",
                 "job_id", "decision", "state")  # fmt: skip
GOAL_ORIGIN_HEADER = b"x-adt4-goal-origin"
OPERATOR_HEADER = b"x-adt4-operator"


def match(method: str, path: str):
    for m, rx, t, names in _COMPILED:
        if m != method:
            continue
        mm = rx.match(path)
        if mm:
            params = dict(zip(names, mm.groups(), strict=False))
            t = t.replace("{1}", mm.group(2)) if "{1}" in t else t
            return t, params
    return None


def _json(raw: bytes):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return {"$text": raw[:2000].decode("utf-8", "replace")}


class RestTap:
    """ASGI middleware: one event per matched operator request (after its response)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        hit = match(scope.get("method", ""), scope.get("path", ""))
        if hit is None:
            return await self.app(scope, receive, send)
        etype, params = hit
        headers = dict(scope.get("headers") or [])
        origin = headers.get(GOAL_ORIGIN_HEADER, b"").decode("latin-1") or None
        operator = headers.get(OPERATOR_HEADER, b"").decode("latin-1") or None
        client = (scope.get("client") or [None])[0]
        body = bytearray()
        resp = bytearray()
        status = {"code": None}
        t0 = time.monotonic()
        t_wall = time.time()

        async def rx():
            msg = await receive()
            if msg.get("type") == "http.request" and len(body) < MAX_BODY:
                body.extend(msg.get("body", b"")[: MAX_BODY - len(body)])
            return msg

        async def tx(msg):
            if msg["type"] == "http.response.start":
                status["code"] = msg.get("status")
            elif msg["type"] == "http.response.body" and len(resp) < MAX_BODY:
                resp.extend(msg.get("body", b"")[: MAX_BODY - len(resp)])
            await send(msg)

        try:
            await self.app(scope, rx, tx)
        finally:
            try:
                self._log(etype, params, bytes(body), bytes(resp), status["code"], t0, origin, operator, client,
                          t_wall)
            except Exception:  # noqa: BLE001
                log.exception("rest tap %s", etype)

    @staticmethod
    def _log(etype, params, body, resp, code, t0, origin, operator, client, t_wall=None) -> None:
        req = _json(body)
        out = _json(resp)
        summary = {k: out.get(k) for k in RESPONSE_KEYS if isinstance(out, dict) and k in out}
        common = {**params, "status": code, "latency_ms": round(1000 * (time.monotonic() - t0), 1),
                  "operator": operator, "client": client}  # fmt: skip
        robot = params.get("robot")
        if etype == "goal":
            kind = params.get("kind") or "direct_path"
            ok = code is not None and code < 400 and not (isinstance(out, dict) and out.get("ok") is False)
            by = origin or "operator"
            gid = summary.get("track")
            # stamped with the request's arrival: the approval precedes goal.sent
            eventlog.emit(SOURCE, "goal.approved", {**common, "kind": kind, "by": by, "body": req}, robot=robot,
                          goal_id=gid, t_wall=t_wall)  # fmt: skip
            if not ok:
                eventlog.emit(SOURCE, "goal.rejected", {**common, "kind": kind, "by": by, "body": req, "response": summary},
                              robot=robot, goal_id=gid)  # fmt: skip
            return
        payload = {**common, "body": req, "response": summary}
        if etype == "op.manip" and isinstance(req, dict):
            payload["decision"] = "approve" if req.get("approve") else ("walk_to" if req.get("walk_to") else "reject")
        if etype.startswith("chat.") and isinstance(req, dict):
            payload["approved"] = req.get("approve")
        eventlog.emit(SOURCE, etype, payload, robot=robot)
