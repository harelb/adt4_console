"""Mission folders: collection, manifest, check and the viewer's queries (plan §5-§7).

A mission lives in ``<base experiment dir>/missions/<mission_id>/`` (the logbook
writes ``mission.json`` / ``reloc.json`` at start).  After the mission:

* :mod:`.collector` slices the base's own runs into ``base/`` and every session
  robot's runs into ``<robot>/`` (:mod:`.slicer` run on the robot over ssh, then
  rsync with a bandwidth limit, deferred while a robot executes a goal; a
  robot that cannot be reached stays ``pending`` and is retried), copies the
  live C7 events into ``<robot>/events.live.jsonl``, writes ``plans/``, exports
  the Neo4j change log (:mod:`.scene_export`), then builds ``manifest.json``
  (:mod:`.manifest`), ``index.sqlite`` (:mod:`.index`) and ``report.json``
  (:mod:`.check`).
* :mod:`.store` lists sessions / missions and answers the Missions page
  (events by time window, goals, chat, stats, replay).
* :mod:`.cli`: ``adt4-mission check|slice|manifest|index|export|list``.

Pure Python; Neo4j and ssh only through the callables the console passes in.
"""

from __future__ import annotations

import bisect
import glob
import json
import os
import pathlib

MISSIONS_DIR = "missions"
SESSIONS_DIR = "sessions"
BASE = "base"
LIVE_FILE = "events.live.jsonl"
PAD_S = 60.0


def read_json(path):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        return None


def write_json(path, data) -> None:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str))
    os.replace(tmp, path)


def iter_jsonl(path):
    """Records of one JSONL file (bad lines skipped)."""
    try:
        f = open(path, encoding="utf-8", errors="replace")  # noqa: SIM115
    except OSError:
        return
    with f:
        for line in f:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                yield rec


def machines(mdir) -> list[str]:
    """The machine folders of a mission dir (``base`` first)."""
    mdir = pathlib.Path(mdir)
    out = []
    for d in sorted(mdir.iterdir()) if mdir.is_dir() else []:
        if d.is_dir() and (
            (d / "slice.json").exists()
            or (d / "events").is_dir()
            or (d / LIVE_FILE).exists()
        ):
            out.append(d.name)
    return sorted(out, key=lambda n: (n != BASE, n))


def event_files(
    mdir, machine: str | None = None
) -> list[tuple[str, str, pathlib.Path]]:
    """``(machine, source, path)`` of every collected event file (+ live copies)."""
    mdir = pathlib.Path(mdir)
    out = []
    for m in [machine] if machine else machines(mdir):
        for p in sorted(glob.glob(str(mdir / m / "events" / "*.jsonl"))):
            out.append((m, pathlib.Path(p).stem, pathlib.Path(p)))
        live = mdir / m / LIVE_FILE
        if live.exists():
            out.append((m, "live", live))
    return out


def all_events(mdir, machine: str | None = None, live: bool = False):
    """``(machine, source, record)`` of the collected events (live copies only
    for machines without a collected slice, unless ``live``)."""
    mdir = pathlib.Path(mdir)
    for m, src, p in event_files(mdir, machine):
        if src == "live" and not live and (mdir / m / "slice.json").exists():
            continue
        for rec in iter_jsonl(p):
            yield m, src, rec


class Offsets:
    """Robot clock minus base clock (s) over time, from the base's
    ``clock.offset`` events; :meth:`to_base` maps a robot ``t_wall``."""

    def __init__(self, samples: dict[str, list[tuple[float, float]]] | None = None):
        self.samples = {r: sorted(v) for r, v in (samples or {}).items()}

    @classmethod
    def from_events(cls, records) -> Offsets:
        s: dict[str, list] = {}
        for rec in records:
            if rec.get("type") != "clock.offset":
                continue
            p = rec.get("payload") or {}
            r, ms = p.get("robot") or rec.get("robot"), p.get("offset_ms")
            if r and ms is not None:
                try:
                    s.setdefault(r, []).append(
                        (float(rec["t_wall"]), float(ms) / 1000.0)
                    )
                except (TypeError, ValueError, KeyError):
                    pass
        return cls(s)

    def at(self, robot: str, t: float) -> float:
        v = self.samples.get(robot)
        if not v:
            return 0.0
        ts = [x[0] for x in v]
        i = bisect.bisect_left(ts, t)
        if i <= 0:
            return v[0][1]
        if i >= len(v):
            return v[-1][1]
        (t0, o0), (t1, o1) = v[i - 1], v[i]
        return o0 if t1 == t0 else o0 + (o1 - o0) * (t - t0) / (t1 - t0)

    def to_base(self, robot: str, t: float) -> float:
        return t - self.at(robot, t)


def mission_window(mission: dict, pad: float = 0.0) -> tuple[float, float]:
    t0 = float(mission.get("t_start") or 0.0)
    t1 = float(mission.get("t_end") or mission.get("t_start") or 0.0)
    return t0 - pad, t1 + pad
