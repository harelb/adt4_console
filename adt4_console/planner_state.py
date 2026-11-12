"""What the planner believes, per robot and for the team, and correcting holding.

The Planner state view puts in one place what decides the next plan:

* **holding** -- what each robot holds. The planner takes it from the map it
  plans on: the heracles publisher writes Neo4j's ``(:Robot)-[:HOLDS]->(:Object)``
  edges into the published scene graph's metadata (``heracles.holding``), and
  omniplanner's world state reads them from there (``holding_from_dsg``). So
  this view reads the same edges from the live Neo4j target. A robot holding an
  object the next goal does not want is made to put it down first.
* the robot's last pose in the map (``Robot.position``, written by
  heracles_state_updater). The planner keeps no at-* facts of its own: where a
  robot is comes from its pose when the goal is planned.
* the persistent constraints the console sends with every goal (ros_bridge).
* the robots in the current plan (omniplanner ``~/plan_robots``) and each
  robot's plan status (goal_manager ``~/robot_plan_states``: running / done /
  failed / stalled / preempted, actions done of total, its goal conjuncts).
* the map version and epoch: heracles' own (``_MapState``) and the planner's
  (omniplanner ``~/map_version``, a count of map changes it has seen, and
  ``~/map_epoch``).
* the last plan error (omniplanner ``~/plan_status`` failures and
  ``~/plan_failed``).

Holding is corrected through heracles_state_updater's
``/<robot>/update_holding_state`` service (heracles_ros_interfaces/
UpdateHoldingState), the path the executors' picks and places use: a release
puts the object down where the map has it now (no robot pose needed), a hold
attaches it to the robot. The state updater bumps the map version, so the
publisher republishes and the planner plans on the corrected holding.
"""

from __future__ import annotations

import collections
import logging
import threading
import time

from .graph_service import parse_symbol

log = logging.getLogger(__name__)

HOLDING_SERVICE_TYPE = "heracles_ros_interfaces/srv/UpdateHoldingState"
# Pushed on the hub when what the planner believes may have changed; the view
# then fetches the snapshot (it needs Neo4j, which the ROS thread must not wait on).
CHANGED_TOPIC = "plan/planner_state"
HOLDING_TIMEOUT_S = 10.0


def holding_service(robot: str) -> str:
    return f"/{robot}/update_holding_state"


def neo4j_symbol(sym: str) -> str:
    """``"O(11)"`` / ``"o11"`` -> ``"O11"`` (heracles' ``nodeSymbol``)."""
    c, i = parse_symbol(sym)
    return f"{c.upper()}{i}"


class HoldingError(ValueError):
    """A holding correction the map cannot take (unknown object, ...)."""


class PlannerState:
    def __init__(self, settings, ros, maps, fleet=None, hub=None):
        self.settings = settings
        self.ros = ros
        self.maps = maps  # map_loader.MapLoader: target() is the live Neo4j
        self.fleet = fleet
        self.hub = hub
        self._lock = threading.Lock()
        # the operator's holding corrections, newest last
        self.corrections: collections.deque = collections.deque(maxlen=20)

    # ---------------------------------------------------------------- reading
    def _source(self):
        return self.maps.target() if self.maps is not None else None

    def _map_state(self, src) -> dict:
        """Holding, robot poses and heracles' map state from the live Neo4j."""
        out = {"source": None, "error": None, "version": None, "epoch": None,
               "rebuilding": False, "rebuild_failed": "", "holding": {}, "poses": {}}  # fmt: skip
        if src is None:
            out["error"] = "no Neo4j target (start the console with --neo4j NAME=URI)"
            return out
        out["source"] = getattr(src, "id", None) or getattr(src, "name", None)
        try:
            rows = src.query(
                "MATCH (r:Robot)-[:HOLDS]->(o:Object) "
                "RETURN r.name AS robot, o.nodeSymbol AS object ORDER BY robot, object"
            )
            for r in rows:
                out["holding"].setdefault(str(r["robot"]), []).append(str(r["object"]))
            for r in src.query("MATCH (r:Robot) RETURN r.name AS n, r.position AS p"):
                p = r["p"]
                if r["n"] and p is not None and hasattr(p, "x"):
                    out["poses"][str(r["n"])] = [round(float(p.x), 2), round(float(p.y), 2), round(float(getattr(p, "z", 0.0) or 0.0), 2)]  # fmt: skip
            rows = src.query(
                "MATCH (m:_MapState) RETURN m.version AS v, m.epoch AS e, "
                "m.rebuilding_since AS t, m.rebuild_failed AS f"
            )
            if rows:
                m = rows[0]
                out["version"] = int(m["v"]) if m["v"] is not None else None
                out["epoch"] = m["e"] or ""
                out["rebuilding"] = m["t"] is not None
                out["rebuild_failed"] = m["f"] or ""
        except Exception as e:  # noqa: BLE001 — GraphUnavailable or a driver error
            out["error"] = str(e)
        return out

    def _roster(self) -> list[str]:
        robots = list(getattr(self.settings, "robots", None) or [])
        if not robots and self.fleet is not None:
            try:
                robots = list(self.fleet.robots())
            except Exception:  # noqa: BLE001
                robots = []
        return robots

    def snapshot(self) -> dict:
        src = None
        map_error = None
        try:
            src = self._source()
        except Exception as e:  # noqa: BLE001 — NotFound: no Neo4j target
            map_error = str(e)
        m = self._map_state(src)
        if map_error and not m["error"]:
            m["error"] = map_error
        ros = self.ros
        view = ros.planner_view() if ros is not None else {}
        states = view.get("robot_plan_states") or {}
        in_plan = view.get("plan_robots")
        errors = ros.last_plan_errors() if ros is not None else {}
        robots = self._roster()
        for r in [*m["holding"], *states, *(in_plan or []), *errors]:
            if r and r not in robots:
                robots.append(r)
        per_robot = []
        for r in robots:
            per_robot.append({
                "robot": r,
                "holding": m["holding"].get(r, []),
                "position": m["poses"].get(r),
                "in_plan": None if in_plan is None else r in in_plan,
                "plan_state": states.get(r),
                "constraints": ros.constraints(r) if ros is not None else [],
                "last_error": errors.get(r),
            })  # fmt: skip
        last = max(errors.values(), key=lambda e: e.get("t") or 0, default=None)
        with self._lock:
            corrections = list(self.corrections)
        return {
            "t": time.time(),
            "ros": bool(ros is not None and ros.available),
            "map": {k: m[k] for k in ("source", "error", "version", "epoch", "rebuilding", "rebuild_failed")},
            "planner": {
                "map_version": view.get("map_version"),
                "map_epoch": view.get("map_epoch"),
                "plan_robots": in_plan,
                "plan_robots_t": view.get("plan_robots_t"),
                "robot_plan_states_t": view.get("robot_plan_states_t"),
                "goal_manager": view.get("robot_plan_states") is not None,
            },
            "robots": per_robot,
            "last_error": last,
            "corrections": corrections,
            "holding_source": "Neo4j HOLDS edges, published to the planner as the map's heracles.holding",
        }  # fmt: skip

    # ------------------------------------------------------------ correcting
    def _object_center(self, src, sym: str):
        rows = src.query(
            "MATCH (o:Object {nodeSymbol: $s}) RETURN o.center AS c, o.class AS k",
            s=sym,
        )
        if not rows:
            return None, None
        c = rows[0]["c"]
        xyz = (
            None
            if c is None
            else (float(c.x), float(c.y), float(getattr(c, "z", 0.0) or 0.0))
        )
        return xyz, rows[0]["k"] or ""

    def set_holding(
        self, robot: str, obj: str | None, author: str = "operator"
    ) -> dict:
        """Make the map say ``robot`` holds ``obj`` (None: nothing).

        Every other object the robot holds is released where the map has it;
        ``obj`` held by another robot is released from that robot first. Each
        step is one ``update_holding_state`` call. Returns {ok, steps, holding}.
        """
        robot = (robot or "").strip()
        if not robot:
            raise HoldingError("no robot given")
        src = self._source()
        want = neo4j_symbol(obj) if obj else None
        m = self._map_state(src)
        if m["error"]:
            raise HoldingError(f"cannot read the map: {m['error']}")
        if want is not None:
            center, _ = self._object_center(src, want)
            if center is None and not src.query(
                "MATCH (o:Object {nodeSymbol: $s}) RETURN o.nodeSymbol AS s", s=want
            ):
                raise HoldingError(f"{want} is not an object in the map")
        held = m["holding"]
        plan = []  # (robot, object, is_holding)
        for o in held.get(robot, []):
            if o != want:
                plan.append((robot, o, False))
        if want is not None:
            for other, objs in held.items():
                if other != robot and want in objs:
                    plan.append((other, want, False))
            if want not in held.get(robot, []):
                plan.append((robot, want, True))
        before = {r: list(v) for r, v in held.items()}
        steps = []
        ok = True
        for r, o, hold in plan:
            pos = None if hold else self._object_center(src, o)[0]
            step = {"robot": r, "object": o, "action": "hold" if hold else "release",
                    "service": holding_service(r)}  # fmt: skip
            try:
                res = self.ros.update_holding_state(
                    r, o, hold, pos, timeout=HOLDING_TIMEOUT_S
                )
                step["ok"] = bool(res.get("success"))
                if not step["ok"]:
                    step["error"] = "heracles_state_updater refused (see its log)"
            except Exception as e:  # noqa: BLE001 — not running / timeout
                step["ok"], step["error"] = False, str(e)
            steps.append(step)
            if not step["ok"]:
                ok = False
                break
        after = self._map_state(src)["holding"]
        what = f"{robot} holds {want}" if want else f"{robot} holds nothing"
        record = {"t": time.time(), "author": author, "request": what, "ok": ok,
                  "before": before, "after": after, "steps": steps}  # fmt: skip
        with self._lock:
            self.corrections.append(record)
        (log.warning if ok else log.error)(
            "Planner state: %s set holding '%s' (%s); before %s, after %s; steps %s",
            author, what, "done" if ok else "FAILED", before, after,
            [(s["action"], s["robot"], s["object"], s["ok"]) for s in steps],
        )  # fmt: skip
        self.notify()
        return {"ok": ok, "request": what, "steps": steps, "holding": after,
                "noop": not plan}  # fmt: skip

    def notify(self) -> None:
        if self.hub is not None:
            self.hub.publish(CHANGED_TOPIC, {"t": time.time()})
