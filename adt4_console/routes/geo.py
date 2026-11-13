"""``/api/geo`` — map anchors and the satellite tile cache (API.md §14)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from . import services

router = APIRouter(prefix="/api/geo")


@router.get("/layers")
def layers(request: Request):
    return {"layers": services(request).geo.tiles.layers()}


@router.get("/anchor")
def default_anchor(request: Request):
    a = services(request).geo.default_anchor()
    return {"anchor": a.to_dict() if a is not None else None}


@router.get("/live_anchor")
def live_anchor(request: Request):
    """The anchor estimated live from the robots' GPS fixes and relocalized
    poses (``anchor``: fit stats, or null), the pairs per robot and why fixes
    were left out."""
    geo = services(request).geo
    if geo.live is None:
        return {"anchor": None, "pairs": {}, "rejected": {}}
    return geo.live.status()


class SaveLiveAnchorBody(BaseModel):
    src: str


@router.post("/live_anchor/save")
def save_live_anchor(body: SaveLiveAnchorBody, request: Request):
    """Write the live anchor as the source's ``geo_anchor.json`` (operator
    action; never automatic). 409 when the source already has a saved anchor."""
    try:
        path = services(request).geo.save_live_anchor(body.src)
    except FileExistsError as e:
        raise HTTPException(409, str(e)) from None
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e)) from None
    return {"ok": True, "path": str(path)}


@router.post("/live_anchor/reset")
def reset_live_anchor(request: Request):
    """Drop the live anchor's pairs (e.g. after the map changed)."""
    geo = services(request).geo
    if geo.live is not None:
        geo.live.reset()
    return {"ok": True}


@router.get("/anchor/{src:path}")
def anchor(src: str, request: Request, cx: float = 0.0, cy: float = 0.0):
    """The source's map→UTM anchor (or ``null``: no GPS alignment), with a local
    affine map↔lon/lat around ``(cx, cy)`` for the browser. Without a saved
    anchor, the live estimate from the robots' GPS (``live``: its fit stats);
    a saved anchor always wins."""
    a, live = services(request).geo.effective_anchor(src)
    if a is None:
        return {"src": src, "anchor": None, "live": None}
    try:
        affine = a.local_affine(cx, cy)
    except Exception as e:  # noqa: BLE001 (pyproj missing)
        raise HTTPException(503, f"geo conversion unavailable: {e}") from None
    return {"src": src, "anchor": a.to_dict(), "affine": affine, "live": live}


@router.get("/tiles/{layer}/{z}/{x}/{y}")
def tile(layer: str, z: int, x: int, y: int, request: Request):
    try:
        data = services(request).geo.tiles.get(layer, z, x, y)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    if data is None:
        raise HTTPException(404, "tile not cached (and live fetching is off)")
    media = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
    return Response(
        data, media_type=media, headers={"Cache-Control": "public, max-age=86400"}
    )
