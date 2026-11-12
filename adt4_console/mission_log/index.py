"""``index.sqlite``: the mission's events in one table, for fast filtering (plan §7).

Built at collect time (and on demand when missing or older than the events):
one row per event with ``t_base`` (the robot's ``t_wall`` mapped to the base
clock), machine, source, type, robot, goal / plan / mission ids and the JSON.
"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3

from . import BASE, Offsets, all_events, event_files

SCHEMA = """
CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  t_base REAL, t_wall REAL, machine TEXT, source TEXT, type TEXT, robot TEXT,
  goal_id TEXT, plan_id TEXT, mission_id TEXT, seq INTEGER, live INTEGER, json TEXT
);
CREATE INDEX ev_t ON events(t_base);
CREATE INDEX ev_type ON events(type);
CREATE INDEX ev_goal ON events(goal_id);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
"""
NAME = "index.sqlite"


def _mtime(mdir) -> float:
    ts = [p.stat().st_mtime for _, _, p in event_files(mdir)]
    return max(ts) if ts else 0.0


def build(mdir) -> pathlib.Path:
    mdir = pathlib.Path(mdir)
    recs = list(all_events(mdir))
    offsets = Offsets.from_events(r for m, _, r in recs if m == BASE)
    tmp = mdir / (NAME + ".tmp")
    if tmp.exists():
        tmp.unlink()
    db = sqlite3.connect(tmp)
    db.executescript(SCHEMA)
    rows = []
    for m, src, r in recs:
        t = float(r.get("t_wall") or 0.0)
        tb = t if m == BASE else offsets.to_base(m, t)
        rows.append((tb, t, m, r.get("source") or src, r.get("type"), r.get("robot"), r.get("goal_id") or None,
                     r.get("plan_id") or None, r.get("mission_id") or None, r.get("seq"), int(src == "live"),
                     json.dumps(r, default=str, separators=(",", ":"))))  # fmt: skip
    db.executemany("INSERT INTO events (t_base, t_wall, machine, source, type, robot, goal_id, plan_id, mission_id,"
                   " seq, live, json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)  # fmt: skip
    db.execute("INSERT INTO meta VALUES ('events_mtime', ?)", (str(_mtime(mdir)),))
    db.commit()
    db.close()
    out = mdir / NAME
    os.replace(tmp, out)
    return out


def ensure(mdir) -> pathlib.Path:
    """The index, rebuilt when an event file is newer than it."""
    mdir = pathlib.Path(mdir)
    p = mdir / NAME
    if p.exists():
        try:
            db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            v = db.execute("SELECT v FROM meta WHERE k='events_mtime'").fetchone()
            db.close()
            if v and float(v[0]) >= _mtime(mdir):
                return p
        except sqlite3.Error:
            pass
    return build(mdir)


def query(mdir, t0=None, t1=None, machine=None, source=None, types=None, goal=None, robot=None, text=None,
          limit=500, offset=0) -> dict:  # fmt: skip
    """``{events, total, t_range}`` ordered by base time; ``types``: exact names or ``prefix.*``."""
    p = ensure(mdir)
    where, args = [], []
    if t0 is not None:
        where.append("t_base >= ?")
        args.append(float(t0))
    if t1 is not None:
        where.append("t_base <= ?")
        args.append(float(t1))
    for col, val in (
        ("machine", machine),
        ("source", source),
        ("goal_id", goal),
        ("robot", robot),
    ):
        if val:
            vals = [v for v in str(val).split(",") if v]
            where.append(f"{col} IN ({','.join('?' * len(vals))})")
            args += vals
    if types:
        ors = []
        for t in [t for t in str(types).split(",") if t]:
            if t.endswith("*"):
                ors.append("type LIKE ?")
                args.append(t[:-1] + "%")
            else:
                ors.append("type = ?")
                args.append(t)
        where.append("(" + " OR ".join(ors) + ")")
    if text:
        where.append("json LIKE ?")
        args.append(f"%{text}%")
    w = (" WHERE " + " AND ".join(where)) if where else ""
    db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        total = db.execute("SELECT count(*) FROM events" + w, args).fetchone()[0]
        rows = db.execute("SELECT t_base, machine, json FROM events" + w + " ORDER BY t_base, id LIMIT ? OFFSET ?",
                          [*args, int(limit), int(offset)]).fetchall()  # fmt: skip
        rng = db.execute("SELECT min(t_base), max(t_base) FROM events").fetchone()
        facets = {
            "machine": dict(
                db.execute(
                    "SELECT machine, count(*) FROM events GROUP BY machine"
                ).fetchall()
            ),
            "source": dict(
                db.execute(
                    "SELECT source, count(*) FROM events GROUP BY source"
                ).fetchall()
            ),
            "type": dict(
                db.execute("SELECT type, count(*) FROM events GROUP BY type").fetchall()
            ),
        }
    finally:
        db.close()
    events = []
    for tb, m, js in rows:
        r = json.loads(js)
        r["t_base"] = tb
        r["_machine"] = m
        events.append(r)
    return {"events": events, "total": total, "t_range": list(rng), "facets": facets, "offset": offset,
            "limit": limit}  # fmt: skip
