"""``/api/quad`` — the Penn quad: status, goals, captures, SAM3 on captures (API.md §16)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from ..quad.link import QuadUnavailable
from ..quad.service import data_uri
from . import services

router = APIRouter(prefix="/api/quad")


def _svc(request: Request):
    q = getattr(services(request), "quad_svc", None)
    if q is None or not q.configured:
        raise HTTPException(
            503, "quad link not configured (--quad-cmd-uri / --quad-image-uri)"
        )
    return q


@router.get("")
def status(request: Request):
    q = getattr(services(request), "quad_svc", None)
    if q is None:
        return {"configured": False}
    return q.state()


class Waypoint(BaseModel):
    x: float  # map frame
    y: float
    query: str = ""


@router.post("/waypoint")
def waypoint(body: Waypoint, request: Request):
    """Task the quad to a map point (no capture on arrival; see ``/goal``)."""
    try:
        return _svc(request).link.send_waypoint(body.x, body.y, body.query)
    except QuadUnavailable as e:
        raise HTTPException(503, str(e)) from None


class Goal(BaseModel):
    x: float  # map frame
    y: float
    capture: bool = True  # take an image on arrival
    query: str = ""  # run SAM3 on that image
    label: str = ""
    chat: dict | None = None  # {session, follow_up}: the chat that asked


@router.post("/goal")
def goal(body: Goal, request: Request):
    try:
        return _svc(request).goal(
            body.x, body.y, body.capture, body.query, body.label, body.chat
        )
    except QuadUnavailable as e:
        raise HTTPException(503, str(e)) from None


class Capture(BaseModel):
    query: str = ""


@router.post("/image")
def image(request: Request, body: Capture | None = None):
    """One image now: the capture (with the map-frame pose) and a data URI."""
    q = _svc(request)
    try:
        c = q.capture(query=(body.query if body else ""))
    except QuadUnavailable as e:
        raise HTTPException(503, str(e)) from None
    return {**q.public_capture(c), "jpeg": data_uri(c["jpeg"])}


@router.post("/calibrate")
def calibrate(request: Request):
    """Takeoff point from the quad's GPS now (also done automatically)."""
    try:
        return _svc(request).calibrate()
    except QuadUnavailable as e:
        raise HTTPException(503, str(e)) from None


@router.get("/captures")
def captures(request: Request):
    q = _svc(request)
    with q._lock:
        items = list(q.captures.values())
    return {"captures": [q.public_capture(c) for c in reversed(items)]}


@router.get("/captures/{cid}.jpg")
def capture_jpeg(cid: str, request: Request):
    data = _svc(request).image(cid)
    if data is None:
        raise HTTPException(404, f"no quad capture {cid}")
    return Response(
        data, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"}
    )


@router.get("/captures/{cid}/overlay.jpg")
def capture_overlay(cid: str, request: Request):
    data = _svc(request).image(cid, overlay=True)
    if data is None:
        raise HTTPException(404, f"no quad capture {cid}")
    return Response(data, media_type="image/jpeg")


class Segment(BaseModel):
    query: str
    conf: float | None = None


@router.post("/captures/{cid}/segment")
def segment(cid: str, body: Segment, request: Request):
    from ..sam3_client import Sam3Error, Sam3Unavailable

    q = _svc(request)
    try:
        out = q.segment(cid, body.query, body.conf)
    except KeyError as e:
        raise HTTPException(404, str(e)) from None
    except (Sam3Unavailable, Sam3Error) as e:
        raise HTTPException(503, str(e)) from None
    return {"capture": q.public_capture(q.get(cid)), "segment": out}
