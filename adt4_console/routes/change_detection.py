"""Active window change detection on the robots (API.md §19).

khronos' RosChangeDetectionController serves ``/<robot>/change_detection/{start,
stop,reset}`` and latches ``~/state``; the ``relocalize_awcd`` configs start it
paused ("press Start once relocalized").  Starting is refused (409) until the
robot is relocalized against the prior map (:func:`relocalized`) — unless the
operator overrides.
"""

from __future__ import annotations

import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..ros_bridge import CHANGE_DETECTION_CMDS, RosUnavailable
from . import services

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

BIG_JUMP_M = 3.0  # frontend lib/frameAlign.ts BIG_JUMP_M / BIG_JUMP_DEG
BIG_JUMP_DEG = 30.0
# map -> odom this far from the initial estimate = aligned (frameAlign.ts MOVED_M / MOVED_DEG)
MOVED_M = 0.5
MOVED_DEG = 2.0
# hydra_multi's status monitor note (hydra_multi_ros/src/status_monitor.cpp)
HYDRA_LC_RE = re.compile(
    r"(\d+)\s*/\s*(\d+)\s+valid\s+inter-?robot\s+loop\s+closures", re.I
)
# ... "; last optimization took 0.412 s, 12.3 s ago" (Hydra feat/hydra-multi-status-optimization)
HYDRA_OPT_RE = re.compile(
    r"last\s+optimization\s+took\s+([\d.]+)\s*s\s*,\s*([\d.]+)\s*s\s+ago", re.I
)
# A robot counts as relocalized once hydra_multi has accepted this many inter-robot
# (prior-map) loop closures for it (frontend lib/frameAlign.ts RELOC_MIN_HYDRA_LCS).
RELOC_MIN_HYDRA_LCS = 5


class CmdBody(BaseModel):
    override: bool = False  # start although the robot is not (known to be) relocalized


class ClearBody(BaseModel):
    robots: list[str] = []


def _hydra_note(health: dict | None) -> str | None:
    """The note of the hydra_multi row of the robot's ros_system_monitor table
    (C1 ``health.nodes``); None when the table or the row is missing."""
    nodes = (health or {}).get("nodes")
    if not isinstance(nodes, list):
        return None
    for n in nodes:
        if not isinstance(n, dict):
            continue
        names = f"{n.get('nickname') or ''} {n.get('node_name') or ''}"
        if "hydra_multi" not in names:
            continue
        return str(n.get("notes") or n.get("note") or "")
    return None


def hydra_matches(health: dict | None) -> tuple[int, int] | None:
    """``(accepted, total)`` inter-robot loop closures from the hydra_multi row
    of the robot's ros_system_monitor table (C1 ``health.nodes``, note
    "N / M valid interrobot loop closures"); None when the table, the row or
    the count is missing (e.g. hydra_multi stale: "time since last spin")."""
    m = HYDRA_LC_RE.search(_hydra_note(health) or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def hydra_optimization(health: dict | None) -> dict | None:
    """``{"took_s", "ago_s"}`` of hydra_multi's last optimization from its
    monitor note ("...; last optimization took 0.412 s, 12.3 s ago"); None when
    the note does not say (no optimization yet, or an older hydra_multi)."""
    m = HYDRA_OPT_RE.search(_hydra_note(health) or "")
    return {"took_s": float(m.group(1)), "ago_s": float(m.group(2))} if m else None


def _yaw_deg(T) -> float:
    _, _, _, qw, qx, qy, qz = T
    return math.degrees(
        math.atan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy * qy + qz * qz))
    )


def _moved(fa: dict, reloc: dict | None) -> str | None:
    """How far ``map -> odom`` moved from the initial estimate, when more than
    optimizer noise: since the agent's first sample (``reloc.total``) or from
    identity (hydra_multi's starting estimate: an agent started after the
    relocalization saw no move).  None otherwise."""
    moves = []
    total = (reloc or {}).get("total")
    if isinstance(total, dict):
        moves.append((float(total.get("dxy") or 0.0), float(total.get("dyaw") or 0.0)))
    T = fa.get("T")
    try:
        if isinstance(T, list | tuple) and len(T) == 7:
            T = [float(v) for v in T]
            moves.append((math.hypot(T[0], T[1]), _yaw_deg(T)))
    except (TypeError, ValueError):
        pass
    for dxy, dyaw in moves:
        if abs(dxy) > MOVED_M or abs(dyaw) > MOVED_DEG:
            return f"{abs(dxy):.1f} m / {dyaw:.0f}°"
    return None


def _suspect(reloc: dict | None) -> str | None:
    """Two or more big ``map -> odom`` jumps: loop closures disagree."""
    reloc = reloc or {}
    jumps = reloc.get("recent_jumps") or (
        [reloc["last_jump"]] if reloc.get("last_jump") else []
    )
    big = [
        j
        for j in jumps
        if j and (j.get("dxy", 0) > BIG_JUMP_M or abs(j.get("dyaw", 0)) > BIG_JUMP_DEG)
    ]
    if len(big) >= 2:
        return f"suspect: the placement jumped {len(big)}x (loop closures disagree)"
    return None


def relocalized(state: dict | None, robot: str = "") -> dict:
    """``{ok, state, reason, source, matches, hydra, roman, optimization}`` from
    the robot's fleetlink C1 state.

    Relocalized (ok) means hydra_multi has accepted at least
    :data:`RELOC_MIN_HYDRA_LCS` inter-robot (prior-map) loop closures for the
    robot (its row of the robot's ``health`` table: "N / M valid interrobot
    loop closures"), and the placement is not suspect (two or more big ``map ->
    odom`` jumps).  Fewer accepted closures is "not_relocalized"
    ("relocalizing").  Without a hydra_multi count, fleetlink's ROMAN counter
    ``reloc.prior_lcs`` and a ``map -> odom`` move away from the initial
    estimate are reported, but they no longer count as relocalized (state
    "aligned" / "unknown", not ok: Start needs an override).  Agents that report
    neither ``reloc`` nor a hydra_multi count (older agents, mapping sessions)
    keep the transform-only verdict.  ``hydra`` = ``{accepted, total, needed}``
    or None, ``roman`` = ROMAN prior-map matches seen by fleetlink (or None),
    ``optimization`` = :func:`hydra_optimization`.  The console's legend adds a
    distance-to-prior-map check the server cannot do."""
    who = robot or "the robot"
    health = (state or {}).get("health")
    hm = hydra_matches(health)
    reloc = (state or {}).get("reloc")
    roman = reloc.get("prior_lcs") if isinstance(reloc, dict) else None
    need = RELOC_MIN_HYDRA_LCS

    def out(ok, st, reason, source=None, matches=None):
        return {
            "ok": ok,
            "state": st,
            "reason": reason,
            "source": source,
            "matches": matches,
            "hydra": {"accepted": hm[0], "total": hm[1], "needed": need}
            if hm
            else None,
            "roman": roman,
            "optimization": hydra_optimization(health),
        }

    if not state:
        return out(False, "unknown", f"unknown: no live state from {who} (fleetlink)")
    fa = state.get("frame_align")
    roman_txt = f"; ROMAN: {roman} prior-map matches seen by fleetlink" if roman else ""
    if hm and hm[0] >= need:
        sus = _suspect(reloc)
        if sus:
            return out(False, "suspect", sus, "hydra_multi", hm[0])
        return out(True, "relocalized", f"relocalized ({hm[0]} accepted prior-map loop closures, hydra_multi {hm[0]} / {hm[1]}; "
                   f"needs {need}){roman_txt}", "hydra_multi", hm[0])  # fmt: skip
    if hm:
        if hm[0] == 0:
            return out(False, "not_relocalized",
                       f"not relocalized: no prior-map match yet (hydra_multi accepted 0 / {hm[1]} inter-robot loop "
                       f"closures, needs {need}; map -> odom is only the initial estimate){roman_txt}",
                       "hydra_multi", 0)  # fmt: skip
        return out(False, "not_relocalized",
                   f"relocalizing: hydra_multi accepted {hm[0]} / {hm[1]} inter-robot loop closures, needs {need}"
                   f"{roman_txt}", "hydra_multi", hm[0])  # fmt: skip
    if not fa:
        return out(
            False, "unknown", f"unknown: {who} reports no frame alignment (older agent)"
        )
    if not fa.get("ok") or fa.get("parent") == fa.get("child"):
        return out(False, "not_relocalized",
                   f"not relocalized: no {fa.get('parent') or 'map'} -> odom transform yet")  # fmt: skip
    if (
        reloc is None
    ):  # older agent / mapping session, no hydra_multi count: the transform decides
        return out(True, "aligned", "aligned to the global frame", "map_odom")
    if roman:
        return out(False, "unknown",
                   f"unknown: no hydra_multi loop-closure count from {who} (needs {need}); ROMAN: {roman} "
                   "prior-map matches seen by fleetlink", "roman", roman)  # fmt: skip
    moved = _moved(fa, reloc)
    if moved:
        return out(False, "aligned",
                   f"aligned (no hydra_multi count, needs {need}): map -> odom moved {moved} from the initial "
                   "estimate", "map_odom")  # fmt: skip
    return out(False, "unknown",
               f"unknown (no relocalization signal from {who}: no hydra_multi loop-closure count in its "
               "status table, no ROMAN prior-map match seen by fleetlink, map -> odom at the initial estimate)")  # fmt: skip


def _status(svc, robot: str) -> dict:
    return {
        **svc.ros.change_detection(robot),
        "relocalized": relocalized(svc.live.state(robot), robot),
    }


@router.get("/robots/{robot}/change_detection")
def get_state(robot: str, request: Request):
    svc = services(request)
    if svc.ros.available:
        svc.ros.ensure_robot(robot)
    return _status(svc, robot)


@router.post("/robots/{robot}/change_detection/{cmd}")
def command(robot: str, cmd: str, request: Request, body: CmdBody | None = None):
    """start / stop / reset, or reset_start (reset, then start: a clean run)."""
    svc = services(request)
    if cmd not in CHANGE_DETECTION_CMDS + ("reset_start",):
        raise HTTPException(404, f"unknown change detection command {cmd}")
    if not svc.ros.available:
        raise RosUnavailable(svc.ros.error or "ROS is off")
    starts = cmd in ("start", "reset_start")
    rel = relocalized(svc.live.state(robot), robot)
    if starts and not rel["ok"] and not (body and body.override):
        return JSONResponse({"detail": f"{robot} is not relocalized: {rel['reason']}", "not_relocalized": True,
                             "reason": rel["reason"]}, status_code=409)  # fmt: skip
    results = _run(svc, robot, cmd)
    return {"ok": all(r["ok"] for r in results), "results": results, "override": bool(starts and not rel["ok"]),
            **_status(svc, robot)}  # fmt: skip


def _run(svc, robot: str, cmd: str) -> list[dict]:
    """One robot's command, step by step (reset_start: reset, then start),
    stopping at the first step that fails."""
    results = []
    for step in ("reset", "start") if cmd == "reset_start" else (cmd,):
        out = svc.ros.change_detection_cmd(robot, step)
        results.append(out)
        if not out["ok"]:
            break
    return results


@router.post("/change_detection/clear")
def clear(body: ClearBody, request: Request):
    """Drop cached changes of these robots (all when empty) at the AWCD base station."""
    svc = services(request)
    if not svc.ros.available:
        raise RosUnavailable(svc.ros.error or "ROS is off")
    return svc.ros.clear_change_detection(body.robots)


@router.get("/change_detection/changes")
def changes(request: Request):
    """The AWCD base station's merged change set (``awcd_changes.py``); hub
    topic ``awcd/changes`` pushes every update.  ``available`` false: ROS is off
    or the workspace has no khronos_msgs; ``topic`` null: no base station seen."""
    svc = services(request)
    if not svc.ros.available or not hasattr(svc.ros, "awcd_changes"):
        return {"available": False, "error": svc.ros.error or "ROS is off", "topic": None, "t": None,
                "frame_id": None, "changes": [], "counts": {"added": 0, "removed": 0}, "robots": {}}  # fmt: skip
    return svc.ros.awcd_changes()


# --------------------------------------------------- fleet-wide reset (§19.3)
class ResetAllBody(BaseModel):
    # keep what change detection already put in the map (scene change writer
    # keep_detections); false: the detectors' empty sets revert it
    keep_applied: bool = True
    robots: list[str] = []  # empty: every robot the console knows


def _candidates(svc) -> list[str]:
    names = set(svc.settings.robots or svc.fleet.robots() or [])
    try:
        names |= set(svc.live.robots())
    except Exception:  # noqa: BLE001
        pass
    if hasattr(svc.ros, "awcd_changes"):
        try:
            names |= set((svc.ros.awcd_changes() or {}).get("robots") or {})
        except Exception:  # noqa: BLE001
            pass
    names.discard("reconciled")
    return sorted(n for n in names if n)


def _sget(svc):
    return getattr(svc.chat, "sget", None)


def _map_plan(svc) -> dict:
    sg = _sget(svc)
    if sg is None:
        return {
            "writer": None,
            "running": False,
            "keep_supported": False,
            "reason": "no SGET service",
        }
    return sg.detections_status()


def _robot_plan(svc, robot: str) -> dict:
    svc.ros.ensure_robot(robot)
    why = svc.ros.change_detection_unreachable(robot)
    return {"robot": robot, "state": svc.ros.change_detection(robot).get("state"), "reachable": why is None,
            "reason": why}  # fmt: skip


@router.get("/change_detection/reset_all")
def reset_all_plan(request: Request):
    """What the fleet-wide Reset would do: every robot (state, reachable or why
    not) and the map side (scene change writer running, keep supported)."""
    svc = services(request)
    if not svc.ros.available:
        return {
            "available": False,
            "error": svc.ros.error or "ROS is off",
            "robots": [],
            "map": _map_plan(svc),
        }
    return {
        "available": True,
        "robots": [_robot_plan(svc, r) for r in _candidates(svc)],
        "map": _map_plan(svc),
    }


@router.post("/change_detection/reset_all")
def reset_all(request: Request, body: ResetAllBody | None = None):
    """Reset change detection everywhere, in this order:

    1. the map (plan repair's scene change writer, SGET): ``keep_applied`` asks
       it to keep what change detection already put in the map
       (``keep_detections``) before the empty change sets arrive; without it
       they revert it.  Operator / chat edits are never touched;
    2. every reachable robot's detector (``/<robot>/change_detection/reset``;
       a running detector keeps running); unreachable robots are listed;
    3. the AWCD base station's cached changes (``clear_robots``, all robots:
       also an offline robot's last report);
    4. the console: pending replan recommendations change detection alone
       caused are superseded.
    """
    svc = services(request)
    body = body or ResetAllBody()
    if not svc.ros.available:
        raise RosUnavailable(svc.ros.error or "ROS is off")
    mp = _map_plan(svc)
    out = {
        "keep_applied": body.keep_applied,
        "map": {**mp, "ok": True, "action": "none"},
    }
    if mp["running"] and body.keep_applied:
        if not mp["keep_supported"]:
            return JSONResponse({"detail": f"the scene change writer ({mp['writer']}) cannot keep detected changes "
                                           "(no keep_detections: older heracles); reset with keep_applied false to "
                                           "revert them, or update heracles", "keep_unsupported": True,
                                 "map": mp}, status_code=409)  # fmt: skip
        kept = _sget(svc).keep_detections()
        out["map"].update(
            action="keep",
            ok=kept["ok"],
            message=kept.get("message"),
            service=kept.get("service"),
        )
        if not kept["ok"]:  # resetting now would revert what the operator asked to keep
            out.update(
                ok=False,
                robots=[],
                base=None,
                error=f"keeping detected changes failed: {kept.get('message')}",
            )
            log.warning("op.awcd_reset aborted: %s", out["error"])
            return out
    elif mp["running"]:
        out["map"].update(action="revert", message="detected changes in the map are reverted when the detectors' "
                                                   "empty change sets reach the scene change writer")  # fmt: skip
    else:
        out["map"]["message"] = (
            mp.get("reason") or "no scene change writer: no map to update"
        )

    wanted = list(dict.fromkeys(body.robots)) or _candidates(svc)
    plans = [_robot_plan(svc, r) for r in wanted]
    todo = [p["robot"] for p in plans if p["reachable"]]
    results = {}
    if todo:
        with ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
            for robot, res in zip(
                todo, pool.map(lambda r: _reset_one(svc, r), todo), strict=True
            ):
                results[robot] = res
    robots = []
    for p in plans:
        r = results.get(p["robot"])
        if r is None:
            robots.append(
                {
                    "robot": p["robot"],
                    "ok": False,
                    "skipped": True,
                    "error": p["reason"],
                }
            )
        else:
            robots.append({"robot": p["robot"], "ok": bool(r.get("ok")), "skipped": False,
                           "message": r.get("message"), "error": r.get("error"), "via": r.get("via")})  # fmt: skip
    out["robots"] = robots
    try:
        base = svc.ros.clear_change_detection([])
    except Exception as e:  # noqa: BLE001
        base = {"ok": False, "error": str(e)}
    out["base"] = base

    done = [r["robot"] for r in robots if r["ok"]]
    text = (f"change detection reset ({'kept' if out['map']['action'] == 'keep' else out['map']['action']} "
            f"detected map changes): robots {', '.join(done) or 'none'}")  # fmt: skip
    sr = svc.scene_repair
    out["scene_repair"] = {
        "dropped": sr.on_detections_reset(text) if sr is not None else 0
    }
    attempted = [r for r in robots if not r["skipped"]]
    out["ok"] = bool(
        out["map"]["ok"]
        and all(r["ok"] for r in attempted)
        and (base.get("ok") or not _has_base(base))
    )
    log.info("op.awcd_reset keep_applied=%s map=%s robots=%s base=%s", body.keep_applied, out["map"]["action"],
             {r["robot"]: ("skipped" if r["skipped"] else r["ok"]) for r in robots}, base.get("ok"))  # fmt: skip
    return out


def _reset_one(svc, robot: str) -> dict:
    try:
        return svc.ros.change_detection_cmd(robot, "reset")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _has_base(base: dict) -> bool:
    """False when the base station simply is not running (not a failure)."""
    return "no awcd_base_station" not in str(base.get("error") or "")


# ------------------------------------------- fleet-wide start / stop (§19.4)
class FleetCmdBody(BaseModel):
    robots: list[str] = []  # empty: every robot the console knows
    override: bool = False  # start robots that are not (known to be) relocalized too


@router.get("/change_detection/fleet")
def fleet_state(request: Request):
    """Every robot's detector for the fleet control: state, reachable (or why
    not) and the relocalization verdict that gates Start."""
    svc = services(request)
    if not svc.ros.available:
        return {
            "available": False,
            "error": svc.ros.error or "ROS is off",
            "robots": [],
        }
    return {"available": True,
            "robots": [{**_robot_plan(svc, r), "relocalized": relocalized(svc.live.state(r), r)}
                       for r in _candidates(svc)]}  # fmt: skip


@router.post("/change_detection/start_all")
def start_all(request: Request, body: FleetCmdBody | None = None):
    """Start change detection on every robot (each as its own Start: reset,
    then start).  A robot that is unreachable, already running or, without
    ``override``, not relocalized is skipped, not a failure."""
    return _fleet_cmd(services(request), "start", body or FleetCmdBody())


@router.post("/change_detection/stop_all")
def stop_all(request: Request, body: FleetCmdBody | None = None):
    """Stop change detection on every reachable robot (never gated)."""
    return _fleet_cmd(services(request), "stop", body or FleetCmdBody())


def _fleet_cmd(svc, cmd: str, body: FleetCmdBody) -> dict:
    if not svc.ros.available:
        raise RosUnavailable(svc.ros.error or "ROS is off")
    rows, todo = [], []
    for robot in list(dict.fromkeys(body.robots)) or _candidates(svc):
        plan = _robot_plan(svc, robot)
        row = {"robot": robot, "ok": False, "skipped": True}
        rel = relocalized(svc.live.state(robot), robot) if cmd == "start" else None
        if not plan["reachable"]:
            row["error"] = plan["reason"]
        elif cmd == "start" and plan["state"] == "running":
            row["error"] = "already running"
        elif rel is not None and not rel["ok"] and not body.override:
            row.update(not_relocalized=True, error=rel["reason"])
        else:
            row["skipped"] = False
            todo.append(robot)
        rows.append(row)
    results = {}
    if todo:
        # each call has the bridge's own timeout: one robot does not hold the others
        step = "reset_start" if cmd == "start" else cmd
        with ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
            for robot, res in zip(
                todo, pool.map(lambda r: _run_one(svc, r, step), todo), strict=True
            ):
                results[robot] = res
    for row in rows:
        res = results.get(row["robot"])
        if res is not None:
            row.update(
                ok=bool(res.get("ok")),
                message=res.get("message"),
                error=res.get("error"),
            )
        row["state"] = svc.ros.change_detection(row["robot"]).get("state")
    counts = {"done": sum(r["ok"] for r in rows), "skipped": sum(r["skipped"] for r in rows),
              "failed": sum(not r["ok"] and not r["skipped"] for r in rows)}  # fmt: skip
    log.info("op.awcd_%s_all override=%s %s", cmd, body.override,
             {r["robot"]: ("skipped" if r["skipped"] else r["ok"]) for r in rows})  # fmt: skip
    return {"ok": counts["failed"] == 0, "cmd": cmd, "robots": rows, "counts": counts}


def _run_one(svc, robot: str, cmd: str) -> dict:
    """The last step's answer of one robot's command; an exception is its error."""
    try:
        return _run(svc, robot, cmd)[-1]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
