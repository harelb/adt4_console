"""Sessions, the relocalization gate and missions (mission logging plan §2).

* **Session**: everything one console Launch starts.  ``session_id =
  s<YYYYmmdd-HHMMSS>_<4hex>``, passed to every ``run-adt4`` as
  ``ADT4_SESSION_ID``; ``sessions/<id>/session.json`` (the launch config,
  redacted, never the env) and ``reloc.json``.  Ends on a new Launch, the active
  launch being cleared, Stop & save of every session robot, or End session.
* **Relocalization phase**: each robot launched to relocalize against a prior map
  goes ``unlocalized`` -> ``relocalizing`` -> ``relocalized`` (or ``failed``); the
  operator confirms it from the Live tab with the evidence in view, or overrides
  it with a reason.  Mapping-mode robots are ``not_required``.  "Start mission"
  is refused until every session robot is relocalized / overridden /
  not required (:meth:`Logbook.gate`).
* **Mission**: ``m<YYYYmmdd-HHMMSS>_<4hex>``, started by the operator or opened
  by the first ``goal.sent`` while none is open (and the gate is satisfied);
  ``missions/<id>/mission.json`` is written at start and on every change, and an
  open mission is reloaded after a console restart (``console.restart``).

Everything lives under the **base experiment dir**:
``<output_root>/<experiment>`` for the session's experiment (the dir the console's
Launch gives the base station's ``run-adt4 -o``: its runs are in ``runs/<run_id>``,
next to ``sessions/`` and ``missions/``), else the experiment dir of
``$ADT4_RUN_DIR`` (``<exp>/runs/<id>``), else ``<output_root>/console``.  Robot
maps copied to the base land beside it as ``<experiment>__<robot>``.  Pure Python: the evidence comes from a
``live_state(robot)`` callable (the fleetlink C1 state), events go to
:mod:`eventlog`, the UI state to the hub topic ``logbook/state``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import pathlib
import re
import secrets
import shutil
import threading
import time

from . import eventlog

log = logging.getLogger(__name__)

HUB_TOPIC = "logbook/state"
# latched std_msgs/String, JSON {mission_id, state: open|closed, session_id}: robots
# (spot_executor, via the fleetlink relay) and base nodes stamp their events with it
MISSION_TOPIC = "/global/mission/current"
SOURCE = "console"
EVIDENCE_S = 10.0  # reloc.evidence every this while relocalizing
DEGRADED_M = 3.0  # map -> odom moved this far since the confirm: reloc.degraded
DEGRADED_DEG = 30.0
PULL_BACK_S = 600.0  # chat turns this long before an auto-opened mission belong to it
GOAL_IMAGES_PER_SYMBOL = 3
GOAL_IMAGE_SYMBOLS = 8

RELOC_STATES = ("unlocalized", "relocalizing", "relocalized", "failed", "overridden", "not_required")
# states that satisfy the mission gate
GATE_OK = ("relocalized", "overridden", "not_required")
SYMBOL_RE = re.compile(r"(?<![A-Za-z0-9_])([OoPpRr])\(?(\d+)\)?(?![A-Za-z0-9_])")


_ACTIVE: Logbook | None = None  # the one Logbook whose hook is installed (latest wins)


class GateClosed(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class Conflict(Exception):
    """A request that does not fit the current state (409)."""


def new_id(prefix: str, t: float | None = None) -> str:
    t = time.time() if t is None else t
    return f"{prefix}{time.strftime('%Y%m%d-%H%M%S', time.localtime(t))}_{secrets.token_hex(2)}"


def _write_json(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(eventlog.redact(data), indent=2, default=str))
    os.replace(tmp, path)


def _read_json(path: pathlib.Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def reloc_required(mode: str | None, prior_path: str | None) -> bool:
    """Relocalize-mode launches (and a manual session given a prior map) need it."""
    if mode == "relocalize":
        return True
    return mode is None and bool(prior_path)


def goal_symbols(fields) -> list[str]:
    """Node symbols a goal names (``o12``, ``O(12)``, ``p850``, ``r3``), canonical ``O(12)``."""
    text = json.dumps(fields, default=str)
    out = []
    for c, i in SYMBOL_RE.findall(text):
        s = f"{c.upper()}({i})"
        if s not in out:
            out.append(s)
    return out[:GOAL_IMAGE_SYMBOLS]


def map_odom(state: dict | None) -> list | None:
    fa = (state or {}).get("frame_align") or {}
    T = fa.get("T")
    if isinstance(T, (list, tuple)) and len(T) == 7:
        try:
            return [round(float(v), 4) for v in T]
        except (TypeError, ValueError):
            return None
    return None


def _yaw_deg(T) -> float:
    _, _, _, qw, qx, qy, qz = T
    return math.degrees(math.atan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy * qy + qz * qz)))


def evidence(state: dict | None, robot: str = "") -> dict:
    """The live relocalization evidence (``routes.change_detection.relocalized``
    plus its parts): hydra_multi accepted / total inter-robot LCs, fleetlink's
    ROMAN ``prior_lcs``, ``map -> odom``."""
    from .routes.change_detection import hydra_matches, relocalized

    hm = hydra_matches((state or {}).get("health"))
    reloc = (state or {}).get("reloc") or {}
    verdict = relocalized(state, robot)
    return {
        "verdict": verdict.get("state"),
        "ok": bool(verdict.get("ok")),
        "reason": verdict.get("reason"),
        "source": verdict.get("source"),
        "hydra_accepted": hm[0] if hm else None,
        "hydra_total": hm[1] if hm else None,
        "roman_prior_lcs": reloc.get("prior_lcs") if isinstance(reloc, dict) else None,
        "map_T_odom": map_odom(state),
        "live": state is not None,
    }


class Logbook:
    def __init__(self, settings, hub=None, *, live_state=None, image_lookup=None, clock=time.time,
                 persist: bool | None = None, state_path: str | None = None):  # fmt: skip
        self.settings = settings
        self.hub = hub
        self.live_state = live_state or (lambda robot: None)
        # sym -> [pathlib.Path] (the images the goal card shows for it); None: no copies
        self.image_lookup = image_lookup
        # (topic, msg_type, fields) -> None: the ROS latched publish (RosBridge.publish_latched)
        self.ros_publish = None
        # (why) -> None at mission start / end: clock.offset samples of every robot
        self.on_mission_edge = None
        self._mission_msg_sent: str | None = None
        self.clock = clock
        self.persist = eventlog.enabled() if persist is None else persist
        self.state_path = pathlib.Path(os.path.expanduser(state_path)) if state_path else None
        self.lock = threading.RLock()
        self.session: dict | None = None
        self.reloc: dict[str, dict] = {}
        self.mission: dict | None = None
        self.last_mission_end: float | None = None
        self.recent: list[dict] = []  # ended missions of this session (newest last)
        self._experiment: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_evidence: dict[str, float] = {}
        self._degraded: dict[str, list] = {}
        self._stop_saved: set[str] = set()
        global _ACTIVE
        if _ACTIVE is not None:
            eventlog.remove_hook(_ACTIVE._on_event)  # noqa: SLF001
        _ACTIVE = self
        eventlog.add_hook(self._on_event)

    # ------------------------------------------------------------ dirs
    def base_dir(self) -> pathlib.Path:
        root = pathlib.Path(os.path.expanduser(self.settings.output_root))
        exp = self._experiment or (self.session or {}).get("experiment")
        if exp:
            # = the base station's run-adt4 -o (launch_workflow.build_base_station_command)
            return root / exp
        rd = os.environ.get("ADT4_RUN_DIR")
        if rd:
            p = pathlib.Path(os.path.expanduser(rd))
            if p.parent.name == "runs":
                return p.parent.parent
            return p
        return root / "console"

    def _repoint(self) -> None:
        """Events / console.log follow the base dir when $ADT4_RUN_DIR is unset."""
        if not self.persist:
            return
        eventlog.set_fallback_root(self.base_dir())
        # moved once attached, or attached now when the cli asked for it before
        # there was any root (no $ADT4_RUN_DIR, no experiment yet)
        if eventlog._console_handler is not None or eventlog._console_wanted:  # noqa: SLF001
            eventlog.attach_console_log()

    def session_dir(self, sid: str | None = None) -> pathlib.Path | None:
        sid = sid or (self.session or {}).get("session_id")
        return self.base_dir() / "sessions" / sid if sid else None

    def mission_dir(self, mid: str | None = None) -> pathlib.Path | None:
        mid = mid or (self.mission or {}).get("mission_id")
        return self.base_dir() / "missions" / mid if mid else None

    # ------------------------------------------------------------ lifecycle
    def start(self, experiment: str | None = None) -> None:
        """Pick the base dir (the active launch's experiment), reload an open
        session / mission after a restart, start the evidence ticker."""
        with self.lock:
            if experiment and not self._experiment:
                self._experiment = experiment
            self._repoint()
            self._reload()
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="logbook", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        eventlog.remove_hook(self._on_event)
        eventlog.flush(2.0)

    def mission_message(self) -> dict:
        m = self.mission
        sid = (self.session or {}).get("session_id") if self.session and self.session.get("state") == "open" else None
        return {"mission_id": (m or {}).get("mission_id") or "", "state": "open" if m else "closed",
                "session_id": (m or {}).get("session_id") or sid or ""}  # fmt: skip

    def publish_mission(self, force: bool = False) -> bool:
        """``/global/mission/current`` (latched) when it changed or ``force``;
        False while ROS is not up (the ticker retries)."""
        if self.ros_publish is None:
            return False
        data = json.dumps(self.mission_message(), sort_keys=True)
        if not force and data == self._mission_msg_sent:
            return True
        try:
            self.ros_publish(MISSION_TOPIC, "std_msgs/msg/String", {"data": data})
        except Exception as e:  # noqa: BLE001 — ROS down / not started yet
            log.debug("mission topic: %s", e)
            return False
        self._mission_msg_sent = data
        return True

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.publish_mission()
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("logbook tick")

    def _save_pointer(self) -> None:
        if not self.persist or self.state_path is None:
            return
        try:
            _write_json(self.state_path, {
                "base_dir": str(self.base_dir()), "experiment": self._experiment,
                "session_id": (self.session or {}).get("session_id") if self.session and self.session.get("state") == "open" else None,
                "mission_id": (self.mission or {}).get("mission_id"),
                "t": self.clock(),
            })  # fmt: skip
        except OSError as e:
            log.warning("logbook pointer %s: %s", self.state_path, e)

    def _reload(self) -> None:
        if not self.persist or self.state_path is None:
            return
        ptr = _read_json(self.state_path) or {}
        if ptr.get("experiment"):
            self._experiment = ptr["experiment"]
            self._repoint()
        sid, mid = ptr.get("session_id"), ptr.get("mission_id")
        if sid:
            s = _read_json(self.base_dir() / "sessions" / sid / "session.json")
            if s and s.get("state") == "open":
                self.session = s
                rl = _read_json(self.base_dir() / "sessions" / sid / "reloc.json") or {}
                self.reloc = rl.get("robots") or {}
                eventlog.set_ids(session_id=sid)
        if mid:
            m = _read_json(self.base_dir() / "missions" / mid / "mission.json")
            if m and m.get("state") == "open":
                self.mission = m
                eventlog.set_ids(mission_id=mid)
        if self.session or self.mission:
            self._emit("console.restart", {
                "session_id": (self.session or {}).get("session_id"),
                "mission_id": (self.mission or {}).get("mission_id"),
                "reloc": {r: v.get("state") for r, v in self.reloc.items()},
            })  # fmt: skip
            self._publish()

    # ------------------------------------------------------------ events
    def _emit(self, type, payload=None, **kw):  # noqa: A002
        return eventlog.emit(SOURCE, type, payload, **kw)

    def _on_event(self, type, payload, kw) -> None:  # noqa: A002
        """eventlog hook: a goal sent with no mission open opens one (gate permitting)."""
        if type != "goal.sent":
            return
        gid = kw.get("goal_id")
        opened = False
        with self.lock:
            if self.mission is None:
                if self.gate()["ok"]:
                    self._start_mission(reason="first_goal", operator=None, name=None, t_first=self.clock())
                    opened = True
                else:
                    log.info("goal sent outside a mission: %s", "; ".join(self.gate()["reasons"]))
            m = self.mission
            if m is not None:
                if gid and gid not in m["goals"]:
                    m["goals"].append(gid)
                for r in [payload.get("robot"), *(payload.get("robots") or [])]:
                    if r and r not in m["robots"]:
                        m["robots"].append(r)
                self._save_mission()
        if self.mission is not None:
            self._copy_goal_images(gid, payload)
        if opened:
            self._publish()

    # ------------------------------------------------------------ session
    def new_session(self, plan: dict, operator: str | None = None) -> str:
        """A console Launch: end the previous session, start a new one."""
        with self.lock:
            if self.session and self.session.get("state") == "open":
                self._stop_session("new_launch")
            t = self.clock()
            sid = new_id("s", t)
            exp = plan.get("experiment") or None
            self._experiment = exp
            self._repoint()
            mode = plan.get("mode")
            robots = list(plan.get("robots") or [])
            required = reloc_required(mode, plan.get("prior_path"))
            self.session = {
                "session_id": sid, "state": "open", "t_start": t, "t_end": None, "end_reason": None,
                "experiment": exp, "mode": mode, "robots": robots, "reloc_required": required,
                "base_station": plan.get("base_station"), "operator": operator,
                "launch": {k: v for k, v in plan.items()},
                "machines": robots + ([plan["base_station"]] if plan.get("base_station") else []),
                "base_dir": str(self.base_dir()),
            }  # fmt: skip
            self.reloc = {}
            self._stop_saved = set()
            self._last_evidence = {}
            self._degraded = {}
            eventlog.set_ids(session_id=sid)
            self._emit("session.start", {"session_id": sid, "experiment": exp, "mode": mode, "robots": robots,
                                         "base_station": plan.get("base_station"), "reloc_required": required})  # fmt: skip
            self._emit("op.launch", {"config": plan, "session_id": sid, "operator": operator})
            for r in robots:
                if required:
                    self.reloc[r] = self._rstate("unlocalized", t)
                else:
                    self.reloc[r] = self._rstate("not_required", t, mode=mode or "mapping")
                    self._emit("reloc.not_required", {"robot": r, "mode": mode or "mapping"}, robot=r)
            self._save_session()
            self._save_reloc()
            self._save_pointer()
        self._publish()
        return sid

    def stop_session(self, reason: str = "operator") -> dict | None:
        with self.lock:
            if not self.session or self.session.get("state") != "open":
                return None
            self._stop_session(reason)
            out = dict(self.session)
        self._publish()
        return out

    def _stop_session(self, reason: str) -> None:
        if self.mission is not None:
            self._end_mission("session_stop")
        s = self.session
        s.update(state="stopped", t_end=self.clock(), end_reason=reason)
        self._emit("session.stop", {"session_id": s["session_id"], "reason": reason,
                                    "duration_s": round(s["t_end"] - s["t_start"], 1)})  # fmt: skip
        self._save_session()
        eventlog.set_ids(session_id=None)
        self._save_pointer()

    def robot_stop_saved(self, robot: str) -> None:
        """Stop & save of one robot; the session stops once every robot is stopped."""
        with self.lock:
            s = self.session
            if not s or s.get("state") != "open" or robot not in s.get("robots", []):
                return
            self._stop_saved.add(robot)
            if set(s["robots"]) <= self._stop_saved:
                self._stop_session("stop_save")
            else:
                return
        self._publish()

    def _save_session(self) -> None:
        d = self.session_dir()
        if not self.persist or d is None:
            return
        try:
            _write_json(d / "session.json", {**self.session, "reloc": {r: v["state"] for r, v in self.reloc.items()}})
        except OSError as e:
            log.warning("session.json: %s", e)

    def _save_reloc(self) -> None:
        d = self.session_dir()
        if not self.persist or d is None:
            return
        try:
            _write_json(d / "reloc.json", self._reloc_record())
        except OSError as e:
            log.warning("reloc.json: %s", e)

    def _reloc_record(self) -> dict:
        return {"session_id": (self.session or {}).get("session_id"), "t": self.clock(), "robots": self.reloc}

    # ------------------------------------------------------------ relocalization
    def _rstate(self, state: str, t: float, **extra) -> dict:
        return {"state": state, "t": t, "history": [{"state": state, "t": t, **extra}], **extra}

    def _robot(self, robot: str) -> dict:
        r = self.reloc.get(robot)
        if r is None:
            raise KeyError(f"{robot} is not in the session" if self.session else "no session: launch from the console first")
        return r

    def _transition(self, robot, to, allowed, event, payload, **extra) -> dict:
        with self.lock:
            r = self._robot(robot)
            if r["state"] not in allowed:
                raise Conflict(f"{robot} is {r['state']}: cannot go to {to}")
            t = self.clock()
            ev = evidence(self.live_state(robot), robot)
            entry = {"state": to, "t": t, **extra, "evidence": ev}
            r.update(state=to, t=t, **extra)
            if to == "relocalized":
                r["confirm_map_T_odom"] = ev["map_T_odom"]
            r["evidence"] = ev
            r["history"].append(entry)
            self._emit(event, {"robot": robot, **payload, **{k: ev[k] for k in (
                "hydra_accepted", "hydra_total", "roman_prior_lcs", "map_T_odom", "verdict")}}, robot=robot)  # fmt: skip
            self._save_reloc()
            self._save_session()
            out = dict(r)
        self._publish()
        return out

    def reloc_start(self, robot: str, operator: str | None = None) -> dict:
        return self._transition(robot, "relocalizing", ("unlocalized", "failed"), "reloc.started",
                                {"operator": operator}, operator=operator, t_started=self.clock())  # fmt: skip

    def reloc_confirm(self, robot: str, operator: str | None = None, method: str = "drive", note: str = "") -> dict:
        with self.lock:
            r = self._robot(robot)
            t_started = r.get("t_started")
            t_first = self._first_match(r)
        return self._transition(
            robot, "relocalized", ("relocalizing",), "reloc.confirmed",
            {"operator": operator, "method": method, "note": note, "t_started": t_started,
             "t_first_match": t_first, "t_confirm": self.clock()},
            operator=operator, method=method, note=note)  # fmt: skip

    def reloc_fail(self, robot: str, operator: str | None = None, note: str = "") -> dict:
        return self._transition(robot, "failed", ("relocalizing",), "reloc.failed",
                                {"operator": operator, "note": note}, operator=operator, note=note)  # fmt: skip

    def reloc_override(self, robot: str, reason: str, operator: str | None = None) -> dict:
        if not (reason or "").strip():
            raise ValueError("an override needs a reason")
        return self._transition(robot, "overridden", ("unlocalized", "relocalizing", "failed"), "reloc.override",
                                {"operator": operator, "reason": reason.strip()}, operator=operator,
                                reason=reason.strip())  # fmt: skip

    @staticmethod
    def _first_match(r: dict) -> float | None:
        for h in r.get("history", []):
            ev = h.get("evidence") or {}
            if (ev.get("hydra_accepted") or 0) > 0 or (ev.get("roman_prior_lcs") or 0) > 0:
                return h.get("t")
        return r.get("t_first_match")

    def gate(self) -> dict:
        """``{ok, reasons, blocking}``: may a mission start?"""
        with self.lock:
            if not self.session or self.session.get("state") != "open":
                return {"ok": True, "reasons": [], "blocking": [], "session": False}
            blocking = [r for r, v in self.reloc.items() if v["state"] not in GATE_OK]
            reasons = [f"{r} is {self.reloc[r]['state']} (confirm its relocalization or override it)" for r in blocking]
            return {"ok": not blocking, "reasons": reasons, "blocking": blocking, "session": True}

    # ------------------------------------------------------------ missions
    def start_mission(self, name: str | None = None, operator: str | None = None) -> dict:
        with self.lock:
            if self.mission is not None:
                raise Conflict(f"mission {self.mission['mission_id']} is already open")
            g = self.gate()
            if not g["ok"]:
                raise GateClosed(g["reasons"])
            self._start_mission(reason="operator", operator=operator, name=name, t_first=None)
            out = dict(self.mission)
        self._publish()
        return out

    def _start_mission(self, reason, operator, name, t_first) -> None:
        t = self.clock()
        mid = new_id("m", t)
        s = self.session if self.session and self.session.get("state") == "open" else None
        pull = None
        if reason == "first_goal":
            pull = max(t - PULL_BACK_S, self.last_mission_end or 0.0)
        self.mission = {
            "mission_id": mid, "name": name or None, "state": "open", "session_id": (s or {}).get("session_id"),
            "experiment": (s or {}).get("experiment"), "t_start": t, "t_end": None, "start_reason": reason,
            "end_reason": None, "operator": operator, "robots": list((s or {}).get("robots") or []), "goals": [],
            "chat_from": pull, "reloc": {r: v["state"] for r, v in self.reloc.items()},
            "run_ids": {eventlog.CONTEXT.machine: eventlog.CONTEXT.run_id},
        }  # fmt: skip
        eventlog.set_ids(mission_id=mid)
        self._emit("mission.start", {"mission_id": mid, "name": name, "reason": reason, "operator": operator,
                                     "session_id": self.mission["session_id"], "robots": self.mission["robots"],
                                     "reloc": self.mission["reloc"], "chat_from": pull})  # fmt: skip
        self._save_mission()
        self._edge("mission.start")
        d = self.mission_dir()
        if self.persist and d is not None:
            try:
                _write_json(d / "reloc.json", self._reloc_record())
            except OSError as e:
                log.warning("mission reloc.json: %s", e)
        self._save_pointer()

    def _edge(self, why: str) -> None:
        if self.on_mission_edge is not None:
            try:
                self.on_mission_edge(why)
            except Exception:  # noqa: BLE001
                log.exception("mission edge")

    def end_mission(self, reason: str = "operator", operator: str | None = None) -> dict:
        with self.lock:
            if self.mission is None:
                raise Conflict("no mission is open")
            out = self._end_mission(reason, operator)
        self._publish()
        return out

    def _end_mission(self, reason: str, operator: str | None = None) -> dict:
        m = self.mission
        t = self.clock()
        m.update(state="ended", t_end=t, end_reason=reason)
        if operator:
            m["ended_by"] = operator
        self._emit("mission.end", {"mission_id": m["mission_id"], "reason": reason, "operator": operator,
                                   "duration_s": round(t - m["t_start"], 1), "goals": list(m["goals"])})  # fmt: skip
        self._save_mission()
        self._edge("mission.end")
        self.recent.append(dict(m))
        self.recent = self.recent[-20:]
        self.mission = None
        self.last_mission_end = t
        eventlog.set_ids(mission_id=None)
        self._save_pointer()
        return m

    def _save_mission(self) -> None:
        d = self.mission_dir()
        if not self.persist or d is None or self.mission is None:
            return
        try:
            _write_json(d / "mission.json", self.mission)
        except OSError as e:
            log.warning("mission.json: %s", e)

    # ------------------------------------------------------------ goal images
    def _copy_goal_images(self, gid, payload) -> None:
        """Copy the images a goal card shows into ``missions/<id>/images/goals/``
        (store frames can disappear later), off the sending thread."""
        if not self.persist or self.image_lookup is None or not gid:
            return
        syms = goal_symbols(payload.get("payload") or payload)
        d = self.mission_dir()
        if not syms or d is None:
            return

        def run():
            refs = []
            for sym in syms:
                try:
                    paths = list(self.image_lookup(sym) or [])[:GOAL_IMAGES_PER_SYMBOL]
                except Exception as e:  # noqa: BLE001
                    refs.append({"sym": sym, "error": str(e)})
                    continue
                for i, p in enumerate(paths):
                    p = pathlib.Path(p)
                    dest = d / "images" / "goals" / str(gid) / f"{sym.replace('(', '').replace(')', '')}_{i}{p.suffix or '.jpg'}"
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(p, dest)
                        refs.append({"sym": sym, "i": i, "src": str(p), "$file": eventlog.rel_ref(dest)})
                    except OSError as e:
                        refs.append({"sym": sym, "i": i, "src": str(p), "error": str(e)})
            self._emit("goal.images", {"goal_id": gid, "refs": refs}, goal_id=gid)

        t = threading.Thread(target=run, name="goal-images", daemon=True)
        t.start()
        self._last_image_thread = t

    # ------------------------------------------------------------ ticker
    def tick(self) -> None:
        """reloc.evidence every EVIDENCE_S while relocalizing; reloc.degraded on a
        big map -> odom move since the confirm while a mission is open."""
        now = self.clock()
        with self.lock:
            robots = [(r, dict(v)) for r, v in self.reloc.items()]
            in_mission = self.mission is not None
        for robot, r in robots:
            if r["state"] == "relocalizing":
                if now - self._last_evidence.get(robot, 0.0) >= EVIDENCE_S:
                    self._last_evidence[robot] = now
                    ev = evidence(self.live_state(robot), robot)
                    with self.lock:
                        if robot in self.reloc:
                            self.reloc[robot]["evidence"] = ev
                    self._emit("reloc.evidence", {"robot": robot, **ev}, robot=robot)
                    self._publish()
            elif r["state"] == "relocalized" and in_mission and r.get("confirm_map_T_odom"):
                T = map_odom(self.live_state(robot))
                T0 = r["confirm_map_T_odom"]
                if T is None:
                    continue
                dxy = math.hypot(T[0] - T0[0], T[1] - T0[1])
                dyaw = abs((_yaw_deg(T) - _yaw_deg(T0) + 180) % 360 - 180)
                if dxy > DEGRADED_M or dyaw > DEGRADED_DEG:
                    last = self._degraded.get(robot)
                    if last and abs(last[0] - dxy) < 0.5 and abs(last[1] - dyaw) < 5:
                        continue  # the same jump, already logged
                    self._degraded[robot] = [dxy, dyaw]
                    self._emit("reloc.degraded", {"robot": robot, "jump_m": round(dxy, 2), "jump_deg": round(dyaw, 1),
                                                  "map_T_odom": T, "confirm_map_T_odom": T0}, robot=robot)  # fmt: skip
                    with self.lock:
                        if robot in self.reloc:
                            self.reloc[robot]["degraded"] = {"t": now, "jump_m": round(dxy, 2), "jump_deg": round(dyaw, 1)}
                            if self.mission is not None:
                                self.mission.setdefault("flags", []).append(
                                    {"t": now, "type": "reloc.degraded", "robot": robot})
                                self._save_mission()
                    self._publish()

    # ------------------------------------------------------------ view
    def view(self) -> dict:
        with self.lock:
            reloc = {}
            for r, v in self.reloc.items():
                reloc[r] = {k: v.get(k) for k in ("state", "t", "operator", "method", "note", "reason", "t_started",
                                                   "degraded", "mode")}  # fmt: skip
                reloc[r]["history"] = [{k: h.get(k) for k in ("state", "t", "operator", "reason", "note")}
                                       for h in v.get("history", [])][-10:]  # fmt: skip
            return {
                "session": self.session,
                "reloc": reloc,
                "gate": self.gate(),
                "mission": self.mission,
                "recent": self.recent[-5:],
                "base_dir": str(self.base_dir()),
                "persist": self.persist,
                "t": self.clock(),
            }

    def view_with_evidence(self) -> dict:
        v = self.view()
        for r in v["reloc"]:
            v["reloc"][r]["evidence"] = evidence(self.live_state(r), r)
        return v

    def _publish(self) -> None:
        self.publish_mission()
        if self.hub is None:
            return
        try:
            self.hub.publish(HUB_TOPIC, self.view())
        except Exception:  # noqa: BLE001
            log.exception("logbook publish")
