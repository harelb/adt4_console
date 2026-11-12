"""Active window change detection on the robots (API.md §19).

khronos' RosChangeDetectionController serves ``/<robot>/change_detection/{start,
stop,reset}`` and latches ``~/state``; the ``relocalize_awcd`` configs start it
paused ("press Start once relocalized").  Starting is refused (409) until the
robot is relocalized against the prior map (:func:`relocalized`) — unless the
operator overrides.
"""

from __future__ import annotations

import math
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..ros_bridge import CHANGE_DETECTION_CMDS, RosUnavailable
from . import services

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


class CmdBody(BaseModel):
    override: bool = False  # start although the robot is not (known to be) relocalized


class ClearBody(BaseModel):
    robots: list[str] = []


def hydra_matches(health: dict | None) -> tuple[int, int] | None:
    """``(accepted, total)`` inter-robot loop closures from the hydra_multi row
    of the robot's ros_system_monitor table (C1 ``health.nodes``, note
    "N / M valid interrobot loop closures"); None when the table, the row or
    the count is missing (e.g. hydra_multi stale: "time since last spin")."""
    nodes = (health or {}).get("nodes")
    if not isinstance(nodes, list):
        return None
    for n in nodes:
        if not isinstance(n, dict):
            continue
        names = f"{n.get('nickname') or ''} {n.get('node_name') or ''}"
        if "hydra_multi" not in names:
            continue
        m = HYDRA_LC_RE.search(str(n.get("notes") or n.get("note") or ""))
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


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
    """``{ok, state, reason, source, matches}`` from the robot's fleetlink C1
    state, using the best evidence there is, in order:

    1. hydra_multi's accepted inter-robot loop closures (its row of the robot's
       ``health`` table: "N / M valid interrobot loop closures"), N > 0;
    2. fleetlink's ROMAN counter ``reloc.prior_lcs`` > 0;
    3. ``map -> odom`` moved away from the initial estimate (weaker: "aligned",
       no match count).

    A positive verdict is "suspect" (not ok) after two or more big ``map ->
    odom`` jumps.  "not relocalized" only when hydra_multi reports its count
    and it is 0; with no count at all the verdict is "unknown" (not ok either:
    Start needs an override).  Agents without ``reloc`` and no hydra_multi
    count: the transform decides (as before).  ``state`` is one of
    relocalized / aligned / suspect / not_relocalized / unknown.  The
    console's legend adds a distance-to-prior-map check the server cannot do."""
    who = robot or "the robot"

    def out(ok, st, reason, source=None, matches=None):
        return {
            "ok": ok,
            "state": st,
            "reason": reason,
            "source": source,
            "matches": matches,
        }

    if not state:
        return out(False, "unknown", f"unknown: no live state from {who} (fleetlink)")
    fa = state.get("frame_align")
    reloc = state.get("reloc")
    hm = hydra_matches(state.get("health"))
    if hm and hm[0] > 0:
        sus = _suspect(reloc)
        if sus:
            return out(False, "suspect", sus, "hydra_multi", hm[0])
        return out(True, "relocalized", f"relocalized ({hm[0]} accepted prior-map matches; hydra_multi {hm[0]} / {hm[1]})",
                   "hydra_multi", hm[0])  # fmt: skip
    if reloc and reloc.get("prior_lcs"):
        sus = _suspect(reloc)
        if sus:
            return out(False, "suspect", sus, "roman", reloc["prior_lcs"])
        return out(True, "relocalized", f"relocalized ({reloc['prior_lcs']} prior-map matches)", "roman",
                   reloc["prior_lcs"])  # fmt: skip
    if not fa:
        return out(
            False, "unknown", f"unknown: {who} reports no frame alignment (older agent)"
        )
    if not fa.get("ok") or fa.get("parent") == fa.get("child"):
        return out(False, "not_relocalized",
                   f"not relocalized: no {fa.get('parent') or 'map'} -> odom transform yet")  # fmt: skip
    moved = _moved(fa, reloc)
    if moved:
        sus = _suspect(reloc)
        if sus:
            return out(False, "suspect", sus, "map_odom")
        return out(True, "aligned", f"aligned (no match count): map -> odom moved {moved} from the initial estimate",
                   "map_odom")  # fmt: skip
    if hm is not None:  # hydra_multi reports its count, and it is 0
        return out(False, "not_relocalized",
                   f"not relocalized: no prior-map match yet (hydra_multi accepted 0 / {hm[1]} inter-robot loop "
                   "closures; map -> odom is only the initial estimate)", "hydra_multi", 0)  # fmt: skip
    if reloc is None:  # older agent, no hydra_multi count: the transform decides
        return out(True, "aligned", "aligned to the global frame", "map_odom")
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
    steps = ("reset", "start") if cmd == "reset_start" else (cmd,)
    results = []
    for step in steps:
        out = svc.ros.change_detection_cmd(robot, step)
        results.append(out)
        if not out["ok"]:
            break
    return {"ok": all(r["ok"] for r in results), "results": results, "override": bool(starts and not rel["ok"]),
            **_status(svc, robot)}  # fmt: skip


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
