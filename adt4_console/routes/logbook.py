"""Sessions, relocalization gate and missions (mission logging; logbook.py).

- ``GET /api/logbook``: session, per-robot relocalization (with live evidence),
  the gate, the open mission (also pushed as hub ``logbook/state``).
- ``POST /api/logbook/mission/start {name?, operator?}``: 409 with the reasons
  while the relocalization gate is closed or a mission is open.
- ``POST /api/logbook/mission/end {operator?}``.
- ``POST /api/logbook/reloc/{robot}/{start|confirm|fail|override}``.
- ``POST /api/logbook/session/stop``.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..logbook import Conflict, GateClosed
from . import services

router = APIRouter(prefix="/api/logbook")


class MissionBody(BaseModel):
    name: str | None = None
    operator: str | None = None


class RelocBody(BaseModel):
    operator: str | None = None
    method: Literal["drive", "initial_pose", "auto"] = "drive"
    note: str = ""
    reason: str = ""  # override


def _lb(request: Request):
    lb = services(request).logbook
    if lb is None:
        raise HTTPException(503, "mission logging is not configured")
    return lb


@router.get("")
def state(request: Request):
    return _lb(request).view_with_evidence()


@router.post("/mission/start")
def mission_start(request: Request, body: MissionBody | None = None):
    lb = _lb(request)
    body = body or MissionBody()
    try:
        m = lb.start_mission(name=(body.name or "").strip() or None, operator=body.operator)
    except GateClosed as e:
        return JSONResponse({"detail": f"relocalization gate: {'; '.join(e.reasons)}", "gate": lb.gate(),
                             "reasons": e.reasons}, status_code=409)  # fmt: skip
    except Conflict as e:
        raise HTTPException(409, str(e)) from None
    return {"mission": m, **lb.view()}


@router.post("/mission/end")
def mission_end(request: Request, body: MissionBody | None = None):
    lb = _lb(request)
    try:
        m = lb.end_mission("operator", operator=(body or MissionBody()).operator)
    except Conflict as e:
        raise HTTPException(409, str(e)) from None
    return {**lb.view(), "ended": m}


@router.post("/reloc/{robot}/{action}")
def reloc(robot: str, action: str, request: Request, body: RelocBody | None = None):
    lb = _lb(request)
    body = body or RelocBody()
    try:
        if action == "start":
            r = lb.reloc_start(robot, body.operator)
        elif action == "confirm":
            r = lb.reloc_confirm(robot, body.operator, body.method, body.note)
        elif action == "fail":
            r = lb.reloc_fail(robot, body.operator, body.note)
        elif action == "override":
            r = lb.reloc_override(robot, body.reason, body.operator)
        else:
            raise HTTPException(404, f"unknown relocalization action {action}")
    except KeyError as e:
        raise HTTPException(404, str(e.args[0] if e.args else e)) from None
    except Conflict as e:
        raise HTTPException(409, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {"robot": robot, "reloc": r, **lb.view()}


@router.post("/session/stop")
def session_stop(request: Request):
    lb = _lb(request)
    s = lb.stop_session("operator")
    if s is None:
        raise HTTPException(409, "no session is open")
    return lb.view()
