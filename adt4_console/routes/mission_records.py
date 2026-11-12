"""Recorded missions (mission logging plan §7; ``mission_log``).

- ``GET /api/logbook/missions``: sessions (launch, robots, relocalization record)
  with their missions (id, name, start, duration, robots, goals done / failed,
  check status, size, collect status), newest first; ``current``: the logbook
  view (open mission, gate).
- ``GET /api/logbook/missions/{id}``: mission.json, manifest, report, collect status,
  reloc, session.
- ``GET .../{id}/events?t0&t1&machine&source&type&goal&robot&q&limit&offset``:
  events by base time (``index.sqlite``), with facets.
- ``GET .../{id}/goals | chat | stats | stats.csv | replay``: the tabs.
- ``GET .../{id}/file?path=``: a file of the mission (images, plans, logs).
- ``POST .../{id}/check``: run ``adt4-mission check`` -> ``report.json``.
- ``POST .../{id}/collect {full?}``: collect now (or re-collect).
- ``POST .../{id}/export {bags?}`` -> ``{file}``; ``GET .../{id}/export/{file}``.
"""

from __future__ import annotations

import os
import pathlib

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from ..mission_log import check as check_mod
from ..mission_log import index as index_mod
from ..mission_log import read_json, store
from . import services

router = APIRouter(prefix="/api/logbook/missions")


def _root(request) -> str:
    return os.path.expanduser(services(request).settings.output_root)


def _mdir(request, mission_id: str) -> pathlib.Path:
    d = store.find_mission(_root(request), mission_id)
    if d is None:
        raise HTTPException(404, f"no mission {mission_id}")
    return d


@router.get("")
def list_missions(request: Request, sizes: bool = True):
    svc = services(request)
    lb = svc.logbook
    return {
        "sessions": store.list_sessions(_root(request), sizes=sizes),
        "current": lb.view() if lb else None,
    }


@router.get("/{mission_id}")
def mission(mission_id: str, request: Request):
    return store.detail(_mdir(request, mission_id))


@router.get("/{mission_id}/events")
def events(mission_id: str, request: Request, t0: float | None = None, t1: float | None = None,
           machine: str | None = None, source: str | None = None, type: str | None = None,  # noqa: A002
           goal: str | None = None, robot: str | None = None, q: str | None = None, limit: int = 500,
           offset: int = 0):  # fmt: skip
    d = _mdir(request, mission_id)
    return index_mod.query(d, t0=t0, t1=t1, machine=machine, source=source, types=type, goal=goal, robot=robot,
                           text=q, limit=max(1, min(limit, 5000)), offset=max(0, offset))  # fmt: skip


@router.get("/{mission_id}/goals")
def goals(mission_id: str, request: Request):
    return {"goals": store.goals(_mdir(request, mission_id))}


@router.get("/{mission_id}/chat")
def chat(mission_id: str, request: Request):
    return {"turns": store.chat(_mdir(request, mission_id))}


@router.get("/{mission_id}/stats")
def stats(mission_id: str, request: Request):
    return store.stats(_mdir(request, mission_id))


@router.get("/{mission_id}/stats.csv")
def stats_csv(mission_id: str, request: Request):
    st = store.stats(_mdir(request, mission_id))
    return PlainTextResponse(store.stats_csv(st), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{mission_id}_goals.csv"'})  # fmt: skip


def _prior_dsg(request, d: pathlib.Path):
    """The session's prior map on this machine (a copy next to the base dir)."""
    m = read_json(d / "mission.json") or {}
    s = (
        read_json(
            d.parent.parent / "sessions" / str(m.get("session_id")) / "session.json"
        )
        or {}
    )
    prior = (s.get("launch") or {}).get("prior_path")
    if not prior:
        return None
    from .. import run_layout

    name = pathlib.Path(os.path.expanduser(prior)).name
    root = pathlib.Path(_root(request))
    for cand in (
        pathlib.Path(os.path.expanduser(prior)),
        root / f"prior_{name}",
        root / name,
    ):
        found = run_layout.find_dsg(cand) if cand.exists() else None
        if found:
            return found[0]
    return None


@router.get("/{mission_id}/replay")
def replay(mission_id: str, request: Request):
    d = _mdir(request, mission_id)
    return store.replay(
        d, prior_dsg=None if (d / "dsg_end.json").exists() else _prior_dsg(request, d)
    )


@router.get("/{mission_id}/file")
def file(mission_id: str, path: str, request: Request):
    d = _mdir(request, mission_id)
    p = store.safe_file(d, path)
    if p is None:
        raise HTTPException(404, f"no file {path} in mission {mission_id}")
    return FileResponse(p)


@router.post("/{mission_id}/check")
def check(mission_id: str, request: Request):
    return check_mod.run(_mdir(request, mission_id))


class CollectBody(BaseModel):
    full: bool = False


@router.post("/{mission_id}/collect")
def collect(mission_id: str, request: Request, body: CollectBody | None = None):
    svc = services(request)
    if svc.collector is None:
        raise HTTPException(503, "mission collection is not configured")
    d = _mdir(request, mission_id)
    return svc.collector.collect(d, full=bool((body or CollectBody()).full))


class ExportBody(BaseModel):
    bags: bool = False


@router.post("/{mission_id}/export")
def export(mission_id: str, request: Request, body: ExportBody | None = None):
    d = _mdir(request, mission_id)
    rep = check_mod.run(d)  # the secret scan runs right before every export
    if rep.get("export_blocked"):
        raise HTTPException(
            409, "the mission check found a secret: export blocked (see the Check tab)"
        )
    out = store.export(
        d, d.parent.parent / "exports", include_bags=bool((body or ExportBody()).bags)
    )
    return {
        "file": out.name,
        "size": out.stat().st_size,
        "path": str(out),
        "check": rep["status"],
    }


@router.get("/{mission_id}/export/{name}")
def export_file(mission_id: str, name: str, request: Request):
    d = _mdir(request, mission_id)
    p = d.parent.parent / "exports" / name
    if not name.startswith(mission_id) or "/" in name or not p.is_file():
        raise HTTPException(404, f"no export {name}")
    return FileResponse(p, filename=name)
