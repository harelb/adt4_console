"""What the planner believes, and correcting what a robot holds (planner_state.py)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..graph_service import GraphUnavailable, NotFound
from ..planner_state import HoldingError
from ..ros_bridge import RosUnavailable
from . import services

router = APIRouter(prefix="/api")


class HoldingBody(BaseModel):
    robot: str
    # the object the robot holds (e.g. "O11" / "o11" / "O(11)"); null: nothing
    object: str | None = None
    # the operator confirmed (the view asks first): the map, and so the next
    # plan, change at once
    confirm: bool = False


def _ps(request: Request):
    ps = services(request).planner_state
    if ps is None:
        raise HTTPException(503, "planner state unavailable")
    return ps


@router.get("/plan/planner_state")
def planner_state(request: Request):
    """Per robot: holding, map pose, in the plan, goal_manager plan state,
    constraints, last plan error; the map / planner versions and epochs."""
    return _ps(request).snapshot()


@router.post("/plan/planner_state/holding")
def set_holding(body: HoldingBody, request: Request):
    """Correct what ``robot`` holds through heracles_state_updater's
    ``/<robot>/update_holding_state`` (releases put the object down where the
    map has it). Needs ``confirm: true``."""
    if not body.confirm:
        raise HTTPException(400, "confirm the holding change (confirm: true)")
    ps = _ps(request)
    ros = services(request).ros
    if not ros.available:
        raise RosUnavailable(ros.error or "not running")
    author = request.headers.get("x-operator") or "operator"
    obj = (body.object or "").strip() or None
    try:
        out = ps.set_holding(body.robot, obj, author=author)
    except (HoldingError, ValueError) as e:
        raise HTTPException(400, str(e)) from None
    except (NotFound, GraphUnavailable) as e:
        raise HTTPException(503, str(e)) from None
    if not out["ok"]:
        failed = next((s for s in out["steps"] if not s["ok"]), {})
        raise HTTPException(
            502,
            f"{failed.get('action', 'update')} {failed.get('object', '')} for "
            f"{failed.get('robot', body.robot)} failed via {failed.get('service', '?')}: "
            f"{failed.get('error', 'no answer')}",
        )
    return out
