"""Live "did the robot get it" status for goals the console sends.

Every goal the console publishes (chat card, auto-send, Plan panel) gets a
track id; the send response carries it and the UI follows ``goal_track/<id>``
on the hub.  The status is built only from what already reaches the console:

* planner: omniplanner ``~/plan_status`` ({plugin, robot, ok, error, t}; it has
  no request id, so the oldest goal still waiting for that robot takes it) and
  each robot's ``compiled_plan_out`` (the first plan for a robot after the goal
  was sent belongs to it; omniplanner publishes every robot's plan before the
  status, so plans just before an ``ok`` join the same goal);
* delivery: fleetlink's ack of the robot's ``compiled_plan_out`` (fleetlink
  mode only);
* executor: ``~/action_result`` (per action, with plan_id), ``~/teleop_state``
  (``plan_executing`` / ``plan_paused``, no plan id) and ``~/runtime_guards``
  (lease owned / owner / known);
* the console's own pause / resume / stop commands.

Pure python (no ROS): ros_bridge feeds the ``on_*`` methods, tests feed fakes.
"""

from __future__ import annotations

import collections
import itertools
import json
import threading
import time

# a goal is matched to plans / statuses for this long after it was sent
MATCH_WINDOW_S = 600.0
# plans this close before an ok plan_status (or after the first plan) are one goal
JOINT_PLAN_S = 5.0
# the executor reports no plan running for this long after progress: stopped
IDLE_S = 8.0
# a teleop_state older than this says nothing
TELEOP_FRESH_S = 5.0
MAX_GOALS = 100
MAX_EVENTS = 40

TERMINAL = ("done", "failed", "preempted")
FAIL_WORDS = ("FAIL", "ABORT", "ERROR", "REJECT", "CANCEL")


def owner_text(owner: str) -> str:
    o = (owner or "").strip()
    if not o:
        return "another client"
    if "spotapp" in o or "android" in o or "tablet" in o.lower():
        return f"the tablet ({o})"
    return o


class RobotTrack:
    def __init__(self, robot: str, named: bool):
        self.robot = robot
        self.named = named  # the goal names it (else: planned along with it)
        self.plan_id: str | None = None
        self.actions: list[str] = []
        self.t_plan: float | None = None
        self.delivered: float | None = None
        self.received: float | None = None
        self.received_how = ""
        self.results: list[dict] = []
        self.paused = False
        self.lease: str | None = None  # None ok | "wait" | "unknown"
        self.lease_owner = ""
        self.idle = False
        self.terminal: str | None = None  # done / failed / preempted
        self.detail = ""
        self.last_progress: float | None = None
        self.events: collections.deque = collections.deque(maxlen=MAX_EVENTS)

    @property
    def n(self) -> int:
        return len(self.actions)

    def done_count(self) -> int:
        return len({r["action_index"] for r in self.results if r["status"] == "SUCCESS"})

    def stage(self) -> tuple[str, str]:
        """(stage, text) for the UI."""
        if self.terminal:
            return self.terminal, self.detail
        if self.plan_id is None:
            return "waiting", "waiting for a plan"
        if self.received is None:
            if self.lease == "wait" and self.delivered is not None:
                return "lease_wait", f"waiting — {owner_text(self.lease_owner)} holds the lease (tablet took over)"
            if self.delivered is not None:
                return "delivered", "plan delivered to the robot (fleetlink ack); executor not heard from yet"
            return "planned", f"planned: {self.n} action{'s' if self.n != 1 else ''}"
        if self.paused:
            return "paused", f"paused at action {self._cur()}/{self.n}"
        if self.lease == "wait":
            return "lease_wait", f"waiting — {owner_text(self.lease_owner)} holds the lease (tablet took over)"
        if self.lease == "unknown":
            return "lease_unknown", f"lease state unknown · action {self._cur()}/{self.n}"
        if self.idle:
            return "stopped", f"not executing — the executor reports no plan running (stopped at action {self._cur()}/{self.n})"
        return "executing", f"executing action {self._cur()}/{self.n} ({self._cur_type()})"

    def _cur(self) -> int:
        return min(self.done_count() + 1, max(self.n, 1))

    def _cur_type(self) -> str:
        i = self._cur() - 1
        return self.actions[i] if 0 <= i < self.n else "?"

    def view(self) -> dict:
        stage, text = self.stage()
        return {
            "robot": self.robot,
            "named": self.named,
            "plan_id": self.plan_id,
            "actions": list(self.actions),
            "n": self.n,
            "done": self.done_count(),
            "t_plan": self.t_plan,
            "delivered": self.delivered,
            "received": self.received,
            "received_how": self.received_how,
            "paused": self.paused,
            "lease": self.lease,
            "lease_owner": self.lease_owner,
            "stage": stage,
            "text": text,
            "events": list(self.events),
        }

    def event(self, t: float, text: str) -> None:
        if not self.events or self.events[-1]["text"] != text:
            self.events.append({"t": t, "text": text})


class Goal:
    def __init__(self, gid, robot, robots, kind, via, summary, t, platforms, team=False):
        self.id = gid
        # a team goal: the planner allocates it across the fleet, so any robot's
        # next plan belongs to it, not only the robots it names
        self.team = team
        self.t = t
        self.robot = robot
        self.kind = kind
        self.via = via
        self.summary = summary
        self.platforms = platforms
        self.planner = "sent"  # sent / planned / failed / skipped
        self.planner_error = ""
        self.planner_t: float | None = None
        self.robots: dict[str, RobotTrack] = {}
        for r in robots:
            self.robots[r] = RobotTrack(r, True)
        self.events: collections.deque = collections.deque(maxlen=MAX_EVENTS)
        self.events.append({"t": t, "text": f"goal sent to {robot}" + (f" via {via}" if via else "")})

    def overall(self) -> tuple[str, str]:
        if self.planner == "failed":
            return "plan_failed", f"planning failed: {self.planner_error}"
        if self.planner == "skipped":
            return "plan_failed", self.planner_error
        tracks = [rt for rt in self.robots.values() if rt.plan_id is not None]
        if not tracks:
            if self.planner == "send_failed":
                return "plan_failed", self.planner_error
            return ("planning", "planning…") if self.planner in ("sent", "direct") else ("planned", "planned (no plan seen for any robot)")
        parts = [f"{rt.robot}: {rt.stage()[1]}" for rt in tracks]
        stages = {rt.stage()[0] for rt in tracks}
        for s in ("failed", "lease_wait", "paused", "stopped", "lease_unknown", "preempted", "executing", "delivered", "planned"):
            if s in stages:
                return s, " · ".join(parts)
        return ("done" if stages == {"done"} else sorted(stages)[0]), " · ".join(parts)

    def _robot_view(self, rt: RobotTrack) -> dict:
        v = rt.view()
        if rt.plan_id is None and self.planner != "sent":
            v["stage"], v["text"] = "no_plan", "no plan for this robot"
        return v

    def view(self) -> dict:
        stage, text = self.overall()
        return {
            "id": self.id,
            "t": self.t,
            "robot": self.robot,
            "kind": self.kind,
            "via": self.via,
            "summary": self.summary,
            "team": self.team,
            "planner": self.planner,
            "planner_error": self.planner_error,
            "planner_t": self.planner_t,
            "stage": stage,
            "text": text,
            "robots": {r: self._robot_view(rt) for r, rt in self.robots.items() if rt.plan_id is not None or rt.named},
            "events": list(self.events),
        }


class GoalTracker:
    def __init__(self, publish=None, clock=time.time):
        self._publish = publish  # (gid, view) -> None
        self.clock = clock
        self._goals: collections.OrderedDict[str, Goal] = collections.OrderedDict()
        self._ids = itertools.count(1)
        self._lock = threading.RLock()
        self._lease: dict[str, dict] = {}
        self._teleop: dict[str, dict] = {}
        self._sent: dict[str, str] = {}  # gid -> last published json

    # ------------------------------------------------------------- queries
    def view(self, gid: str) -> dict | None:
        with self._lock:
            g = self._goals.get(gid)
            return g.view() if g is not None else None

    def recent(self, n: int = 20) -> list[dict]:
        with self._lock:
            return [g.view() for g in list(self._goals.values())[-n:]]

    # ------------------------------------------------------------- feeding
    def goal_sent(self, robot, robots=None, kind="", via=None, summary="", t=None, platforms=None, direct=False,
                  team=False) -> str:
        """Register a goal before it is published; ``direct``: the console
        publishes the plan itself (no planner, no plan_status); ``team``: the
        planner allocates it across the fleet (any robot's plan joins it)."""
        t = self.clock() if t is None else t
        with self._lock:
            gid = f"g{int(t)}-{next(self._ids)}"
            g = Goal(gid, robot, list(dict.fromkeys(robots or [robot])), kind, via, summary, t, platforms or {}, team)
            if direct:
                g.planner = "direct"
            self._goals[gid] = g
            while len(self._goals) > MAX_GOALS:
                old = self._goals.popitem(last=False)[0]
                self._sent.pop(old, None)
            self._emit(g)
            return gid

    def send_failed(self, gid: str, error: str) -> None:
        """The publish itself failed (nothing was sent)."""
        with self._lock:
            g = self._goals.get(gid)
            if g is not None:
                g.planner, g.planner_error = "send_failed", f"not sent: {error}"
                g.events.append({"t": self.clock(), "text": g.planner_error})
                self._emit(g)

    def _live(self, t):
        return [g for g in self._goals.values() if t - g.t < MATCH_WINDOW_S]

    def on_plan(self, robot: str, plan: dict, t=None) -> None:
        """A compiled plan for ``robot`` ({plan_id, actions: [{type}]})."""
        t = self.clock() if t is None else t
        pid = str(plan.get("plan_id") or "")
        actions = [str(a.get("type") or "?") for a in plan.get("actions") or []]
        with self._lock:
            owner = self._claimant(robot, t)
            for g in self._live(t):
                rt = g.robots.get(robot)
                if g is owner or rt is None or rt.plan_id is None or rt.plan_id == pid or rt.terminal in ("done", "failed"):
                    continue
                if rt.terminal == "preempted":
                    continue
                if owner is not None and owner.t > g.t:
                    rt.terminal, rt.detail = "preempted", f"preempted by a newer goal (plan {pid[:8]})"
                    rt.event(t, rt.detail)
                    self._emit(g)
                elif owner is None and rt.terminal is None:
                    # not from a console goal: plan repair replanned it, or another sender
                    old = rt.plan_id
                    self._attach(rt, pid, actions, t)
                    rt.event(t, f"replaced: plan {old[:8]} → {pid[:8]} (plan repair replan or another sender), {len(actions)} actions")
                    self._emit(g)
            if owner is not None:
                rt = owner.robots.get(robot)
                if rt is None:
                    rt = owner.robots[robot] = RobotTrack(robot, False)
                if rt.plan_id != pid:
                    self._attach(rt, pid, actions, t)
                    rt.event(t, f"planned: plan {pid[:8]}, {len(actions)} action{'s' if len(actions) != 1 else ''} ({', '.join(actions) or 'none'})")
                    if not actions:
                        rt.terminal, rt.detail = "done", "empty plan: nothing to do"
                    if owner.planner == "direct":
                        owner.planner, owner.planner_t = "planned", t
                    elif owner.planner == "sent" and owner.planner_t is None:
                        owner.planner_t = t  # the first plan: others join for JOINT_PLAN_S
                    self._emit(owner)

    def _attach(self, rt: RobotTrack, pid, actions, t):
        rt.plan_id, rt.actions, rt.t_plan = pid, actions, t
        rt.delivered = rt.received = None
        rt.received_how = ""
        rt.results = []
        rt.paused = rt.idle = False
        rt.terminal, rt.detail = None, ""
        rt.last_progress = t
        lease = self._lease.get(rt.robot)
        if lease is not None:
            self._apply_lease(rt, lease, t, quiet=True)

    def _claimant(self, robot, t) -> Goal | None:
        """The goal a new plan for ``robot`` belongs to: the newest goal still
        planning that names the robot, or any goal still planning (multi-robot
        planners give other robots work), or a goal whose plan_status came in
        a moment ago / whose first plan just came."""
        live = self._live(t)
        for g in reversed(live):
            rt = g.robots.get(robot)
            if g.planner in ("sent", "direct") and rt is not None and rt.plan_id is None:
                return g
        # a team goal still planning: the planner may give the work to any robot
        newest = live[-1] if live else None
        if newest is not None and newest.team and newest.planner == "sent":
            rt = newest.robots.get(robot)
            if rt is None or rt.plan_id is None:
                return newest
        for g in reversed(live):
            if g.planner == "sent" and robot not in g.robots and g.planner_t is not None and t - g.planner_t < JOINT_PLAN_S:
                return g
        for g in reversed(live):
            if g.planner == "planned" and g.planner_t is not None and t - g.planner_t < JOINT_PLAN_S:
                rt = g.robots.get(robot)
                if rt is None or rt.plan_id is None:
                    return g
        return None

    def on_plan_status(self, st: dict, t=None) -> None:
        """omniplanner ``~/plan_status``: {plugin, robot, ok, error, t}."""
        t = self.clock() if t is None else t
        robot = str(st.get("robot") or "")
        with self._lock:
            waiting = [g for g in self._live(t) if g.planner == "sent" and (not robot or g.robot == robot or robot in g.robots)]
            if not waiting:
                return
            # FIFO: the oldest waiting goal for the robot; older ones were skipped
            g = waiting[0]
            # a goal that has plans already is the one this status is about
            for w in waiting:
                if any(rt.plan_id for rt in w.robots.values()):
                    g = w
                    break
            for w in waiting:
                if w is g:
                    break
                w.planner = "skipped"
                w.planner_error = "no plan: the planner skipped this goal for a newer one"
                w.events.append({"t": t, "text": w.planner_error})
                self._emit(w)
            if st.get("ok"):
                g.planner = "planned"
                g.planner_t = t
                robots = [r for r, rt in g.robots.items() if rt.plan_id]
                g.events.append({"t": t, "text": "planner: ok" + (f" ({', '.join(robots)})" if robots else "")})
            else:
                g.planner = "failed"
                g.planner_t = t
                g.planner_error = str(st.get("error") or "unknown error")
                g.events.append({"t": t, "text": f"planning failed: {g.planner_error}"})
            self._emit(g)

    def _tracks(self, robot, t, plan_id=None):
        for g in self._live(t):
            rt = g.robots.get(robot)
            if rt is None or rt.plan_id is None:
                continue
            if plan_id is not None and rt.plan_id != plan_id:
                continue
            yield g, rt

    def on_delivered(self, robot: str, t=None) -> None:
        """fleetlink acked a ``compiled_plan_out`` message on the robot."""
        t = self.clock() if t is None else t
        with self._lock:
            for g, rt in self._tracks(robot, t):
                if rt.delivered is None and rt.terminal is None and t >= rt.t_plan:
                    rt.delivered = t
                    rt.event(t, "delivered to the robot (fleetlink ack)")
                    self._emit(g)

    def on_result(self, robot: str, res: dict, t=None) -> None:
        """``~/action_result``: {plan_id, action_index, action_type, status, detail}."""
        t = self.clock() if t is None else t
        pid = str(res.get("plan_id") or "")
        status = str(res.get("status") or "").upper()
        idx = int(res.get("action_index") or 0)
        with self._lock:
            for g, rt in self._tracks(robot, t, pid):
                self._received(rt, t, "action result")
                rt.results.append({"action_index": idx, "status": status, "t": t})
                rt.last_progress = t
                rt.idle = False
                what = f"action {idx + 1}/{rt.n} ({res.get('action_type') or '?'})"
                detail = str(res.get("detail") or "")
                if status == "SUCCESS":
                    rt.event(t, f"{what} done")
                    if rt.done_count() >= rt.n:
                        rt.terminal, rt.detail = "done", f"done: all {rt.n} actions succeeded"
                        rt.event(t, rt.detail)
                elif status == "PREEMPTED":
                    rt.terminal, rt.detail = "preempted", f"preempted at {what}" + (f": {detail}" if detail else "")
                    rt.event(t, rt.detail)
                elif any(w in status for w in FAIL_WORDS):
                    rt.terminal, rt.detail = "failed", f"failed at {what}" + (f": {detail}" if detail else "")
                    rt.event(t, rt.detail)
                else:
                    rt.event(t, f"{what}: {status}" + (f" ({detail})" if detail else ""))
                self._emit(g)

    def _received(self, rt: RobotTrack, t, how) -> bool:
        if rt.received is not None:
            return False
        rt.received, rt.received_how = t, how
        rt.last_progress = t
        rt.event(t, f"received by {rt.robot} ({how})")
        return True

    def on_teleop(self, robot: str, st: dict, t=None) -> None:
        """The executor's ``~/teleop_state`` (plan_executing / plan_paused)."""
        t = self.clock() if t is None else t
        self._teleop[robot] = st
        executing = bool(st.get("plan_executing"))
        paused = bool(st.get("plan_paused"))
        last_event = str(st.get("last_event") or "")
        with self._lock:
            for g, rt in self._tracks(robot, t):
                if rt.terminal is not None:
                    continue
                changed = False
                if rt.received is None:
                    exact = rt.plan_id and last_event == f"new plan {rt.plan_id} received"
                    # without fleetlink's ack the executor may still be on the previous plan
                    ok = exact or ((executing or paused) and (rt.delivered is not None or not g.platforms.get("fleetlink_" + robot)))
                    if ok and t >= (rt.t_plan or 0):
                        changed |= self._received(rt, t, "executor reports the new plan" if exact else "executor reports a plan running")
                if rt.received is None:
                    if changed:
                        self._emit(g)
                    continue
                if paused != rt.paused:
                    rt.paused = paused
                    rt.event(t, "paused" if paused else "resumed")
                    rt.last_progress = t
                    changed = True
                if executing or paused:
                    rt.last_progress = t
                idle = not executing and not paused and t - (rt.last_progress or t) > IDLE_S
                if idle != rt.idle:
                    rt.idle = idle
                    rt.event(t, "executor reports no plan running (stopped, or it gave up waiting)" if idle else "executing again")
                    changed = True
                if changed:
                    self._emit(g)

    def on_lease(self, robot: str, lease: dict, t=None) -> None:
        """The executor's runtime guards: {known, owned, owner}."""
        t = self.clock() if t is None else t
        with self._lock:
            self._lease[robot] = dict(lease)
            for g, rt in self._tracks(robot, t):
                if rt.terminal is None and self._apply_lease(rt, lease, t):
                    self._emit(g)

    def _apply_lease(self, rt: RobotTrack, lease: dict, t, quiet=False) -> bool:
        if not lease.get("known"):
            new, owner = "unknown", ""
        elif lease.get("owned"):
            new, owner = None, ""
        else:
            new, owner = "wait", str(lease.get("owner") or "")
        if (new, owner) == (rt.lease, rt.lease_owner):
            return False
        old = rt.lease
        rt.lease, rt.lease_owner = new, owner
        if not quiet:
            if new == "wait":
                rt.event(t, f"{owner_text(owner)} took the lease: the robot waits")
            elif new == "unknown":
                rt.event(t, "lease state unknown")
            elif old is not None:
                rt.event(t, "lease back with the executor")
        rt.last_progress = t
        return True

    def on_executor_cmd(self, robot: str, cmd: str, ok: bool = True, t=None) -> None:
        """The console sent pause / resume / stop to the robot's executor."""
        t = self.clock() if t is None else t
        with self._lock:
            for g, rt in self._tracks(robot, t):
                if rt.terminal is None:
                    rt.event(t, f"{cmd} sent from the console" + ("" if ok else " (not delivered)"))
                    if cmd == "stop" and ok:
                        rt.terminal, rt.detail = "preempted", "stopped from the console"
                    self._emit(g)

    # ------------------------------------------------------------- output
    def _emit(self, g: Goal) -> None:
        if self._publish is None:
            return
        v = g.view()
        key = json.dumps(v, sort_keys=True, default=str)
        if self._sent.get(g.id) == key:
            return
        self._sent[g.id] = key
        try:
            self._publish(g.id, v)
        except Exception:  # noqa: BLE001
            pass

    def summary_lines(self, n: int = 5) -> list[str]:
        """One line per recent goal (for the chat model's context)."""
        with self._lock:
            out = []
            for g in list(self._goals.values())[-n:]:
                stage, text = g.overall()
                out.append(f"{g.id} ({g.kind} to {g.robot}, {g.summary[:80]}): {stage} — {text[:200]}")
            return out
