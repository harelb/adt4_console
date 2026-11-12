"""The Missions page's queries over mission folders (plan §7).

* :func:`list_sessions`: every ``<output_root>/<experiment>/sessions/*`` with its
  missions (``missions/*``), newest first; missions without a session are grouped
  under ``session_id: null``.
* :func:`detail`: mission.json, manifest, report, collect status, reloc, session.
* :func:`goals`, :func:`chat`, :func:`stats`, :func:`replay`: the tabs.
* :func:`export`: a ``.tar.zst`` (``.tar.gz`` without zstd) of the folder, bags
  only on request; refused while the check found a secret.
"""

from __future__ import annotations

import json
import math
import pathlib
import re
import shutil
import subprocess
import tarfile

from . import (
    BASE,
    MISSIONS_DIR,
    SESSIONS_DIR,
    Offsets,
    all_events,
    iter_jsonl,
    read_json,
)

ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
MAP_NODES_MAX = 4000


def _dir_size(p: pathlib.Path) -> int:
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def find_mission(output_root, mission_id: str) -> pathlib.Path | None:
    if not ID_RE.match(mission_id or ""):
        return None
    root = pathlib.Path(output_root).expanduser()
    hits = sorted(
        root.glob(f"*/{MISSIONS_DIR}/{mission_id}"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return hits[0] if hits else None


def summary(mdir: pathlib.Path, sizes: bool = True) -> dict:
    m = read_json(mdir / "mission.json") or {}
    rep = read_json(mdir / "report.json") or {}
    col = read_json(mdir / "collect.json") or {}
    man = read_json(mdir / "manifest.json") or {}
    goals = man.get("goals") or []
    done = sum(
        1
        for g in goals
        if any(
            (o or {}).get("result") == "done"
            for o in (g.get("outcomes") or {}).values()
        )
    )
    failed = sum(1 for g in goals if any((o or {}).get("result") in ("failed", "plan_failed", "preempted")
                                         for o in (g.get("outcomes") or {}).values()))  # fmt: skip
    t0, t1 = m.get("t_start"), m.get("t_end")
    return {
        "mission_id": m.get("mission_id") or mdir.name,
        "name": m.get("name"),
        "state": m.get("state"),
        "session_id": m.get("session_id"),
        "experiment": m.get("experiment") or mdir.parent.parent.name,
        "t_start": t0,
        "t_end": t1,
        "duration_s": round(t1 - t0, 1) if t0 and t1 else None,
        "robots": m.get("robots") or [],
        "n_goals": len(m.get("goals") or []) or len(goals),
        "goals_done": done,
        "goals_failed": failed,
        "check": rep.get("status"),
        "check_counts": rep.get("counts"),
        "collect": col.get("state"),
        "collect_machines": {
            k: v.get("state") for k, v in (col.get("machines") or {}).items()
        },
        "size": _dir_size(mdir) if sizes else None,
        "end_reason": m.get("end_reason"),
        "path": str(mdir),
    }


def list_sessions(output_root, sizes: bool = True) -> list[dict]:
    root = pathlib.Path(output_root).expanduser()
    sessions: dict = {}
    for sj in root.glob(f"*/{SESSIONS_DIR}/*/session.json"):
        s = read_json(sj) or {}
        sid = s.get("session_id") or sj.parent.name
        rl = read_json(sj.parent / "reloc.json") or {}
        sessions[sid] = {
            "session_id": sid, "experiment": s.get("experiment") or sj.parent.parent.parent.name,
            "state": s.get("state"), "t_start": s.get("t_start"), "t_end": s.get("t_end"),
            "robots": s.get("robots") or [], "mode": s.get("mode"), "base_station": s.get("base_station"),
            "launch": {k: (s.get("launch") or {}).get(k) for k in ("mode", "experiment", "prior_path", "network",
                                                                  "base_station", "robots", "config")},
            "reloc": {r: {k: v.get(k) for k in ("state", "t", "operator", "method", "reason", "note")}
                      for r, v in (rl.get("robots") or {}).items()},
            "missions": [],
        }  # fmt: skip
    loose = {
        "session_id": None,
        "experiment": None,
        "missions": [],
        "robots": [],
        "reloc": {},
    }
    for mj in root.glob(f"*/{MISSIONS_DIR}/*/mission.json"):
        s = summary(mj.parent, sizes)
        (sessions.get(s["session_id"]) or loose)["missions"].append(s)
    out = sorted(sessions.values(), key=lambda s: -(s.get("t_start") or 0))
    for s in out + [loose]:
        s["missions"].sort(key=lambda x: -(x.get("t_start") or 0))
    return out + ([loose] if loose["missions"] else [])


def detail(mdir: pathlib.Path) -> dict:
    m = read_json(mdir / "mission.json") or {}
    sid = m.get("session_id")
    sess = (
        read_json(mdir.parent.parent / SESSIONS_DIR / sid / "session.json")
        if sid
        else None
    )
    if sess:
        sess = {k: v for k, v in sess.items() if k != "launch"} | {
            "launch": {
                k: (sess.get("launch") or {}).get(k)
                for k in ("mode", "experiment", "prior_path", "robots")
            }
        }
    return {
        "summary": summary(mdir),
        "mission": m,
        "session": sess,
        "session_reloc": read_json(
            mdir.parent.parent / SESSIONS_DIR / sid / "reloc.json"
        )
        if sid
        else None,
        "reloc": read_json(mdir / "reloc.json"),
        "manifest": read_json(mdir / "manifest.json"),
        "report": read_json(mdir / "report.json"),
        "collect": read_json(mdir / "collect.json"),
        "map_state": read_json(mdir / "map_state.json"),
        "has_dsg": (mdir / "dsg_end.json").exists(),
    }


# ------------------------------------------------------------------ tabs
def _events(mdir):
    recs = [(m, s, r) for m, s, r in all_events(mdir)]
    offs = Offsets.from_events(r for m, _, r in recs if m == BASE)
    for m, s, r in recs:
        t = float(r.get("t_wall") or 0.0)
        r["t_base"] = t if m == BASE else offs.to_base(m, t)
    return recs, offs


def _poses(recs) -> dict[str, list]:
    """robot -> [[t, x, y, yaw]] (the base's robot.pose; a robot's own log when
    the base has none for it)."""
    base: dict[str, list] = {}
    own: dict[str, list] = {}
    for m, _s, r in recs:
        if r.get("type") != "robot.pose":
            continue
        p = r.get("payload") or {}
        if p.get("x") is None or p.get("y") is None:
            continue
        row = [round(r["t_base"], 2), float(p["x"]), float(p["y"]), p.get("yaw")]
        if m == BASE:
            base.setdefault(p.get("robot") or r.get("robot"), []).append(row)
        else:
            own.setdefault(m, []).append(row)
    for robot, rows in own.items():
        base.setdefault(robot, rows)
    return {k: sorted(v) for k, v in base.items() if k}


def _driven(rows, a, b) -> float:
    pts = [(x, y) for t, x, y, _ in rows if a <= t <= b]
    return round(
        sum(math.hypot(x2 - x1, y2 - y1) for (x1, y1), (x2, y2) in zip(pts, pts[1:])), 2
    )


def goals(mdir: pathlib.Path) -> list[dict]:
    recs, _ = _events(mdir)
    base = [r for m, _, r in recs if m == BASE]
    poses = _poses(recs)
    solved = {}
    for r in base:
        if r.get("type") == "plan.solved":
            p = r.get("payload") or {}
            for pid in {
                r.get("plan_id"),
                p.get("plan_id"),
                *((p.get("plan_ids") or {}).values()),
            }:
                if pid:
                    solved[pid] = p
    out: dict = {}
    for r in base:
        gid = r.get("goal_id")
        t = r.get("type")
        p = r.get("payload") or {}
        if not gid:
            continue
        if t == "goal.proposed":
            out.setdefault(gid, {"goal_id": gid}).update(card=p.get("card") or p)
        elif t == "goal.sent":
            g = out.setdefault(gid, {"goal_id": gid})
            g.update(t_sent=r["t_base"], robot=p.get("robot"), kind=p.get("kind"), payload=p.get("payload"),
                     sent=p)  # fmt: skip
        elif t in ("goal.approved", "goal.rejected"):
            out.setdefault(gid, {"goal_id": gid})[t.split(".")[1]] = {
                "t": r["t_base"],
                **p,
            }
        elif t == "goal.stage":
            out.setdefault(gid, {"goal_id": gid}).setdefault("stages", []).append(
                {"t": r["t_base"], "robot": p.get("robot"), "from": p.get("from"), "to": p.get("to"),
                 "detail": p.get("detail"), "plan_id": p.get("plan_id")})  # fmt: skip
        elif t == "goal.outcome":
            out.setdefault(gid, {"goal_id": gid}).setdefault("outcomes", {})[
                p.get("robot") or ""
            ] = {"t": r["t_base"], **p}
        elif t == "goal.images":
            out.setdefault(gid, {"goal_id": gid}).setdefault("images", []).extend(
                p.get("refs") or []
            )
    rob_events = [(m, r) for m, _, r in recs if m != BASE]
    for g in out.values():
        plan_ids = {
            s["robot"]: s["plan_id"]
            for s in g.get("stages", [])
            if s.get("robot") and s.get("plan_id")
        }
        g["plan_ids"] = plan_ids
        per = {}
        for robot, pid in plan_ids.items():
            sp = solved.get(pid) or {}
            t_ex = [
                s["t"]
                for s in g.get("stages", [])
                if s.get("robot") == robot and s.get("to") == "executing"
            ]
            o = (g.get("outcomes") or {}).get(robot) or (g.get("outcomes") or {}).get(
                ""
            )
            t_end = (o or {}).get("t")
            per[robot] = {
                "plan_id": pid,
                "engine": sp.get("engine"),
                "solve_s": sp.get("solve_s"),
                "planned_m": (sp.get("path_len_m") or {}).get(robot),
                "driven_m": _driven(poses.get(robot, []), t_ex[0], t_end or 1e18) if t_ex else None,
                "time_to_reach_s": round(t_end - g["t_sent"], 1) if t_end and g.get("t_sent") else None,
                "result": (o or {}).get("result"),
                "leases": sum(1 for m, r in rob_events if m == robot and r.get("type") == "lease.intervention"
                              and r.get("plan_id") == pid),
                "stalls": sum(1 for r in base if r.get("type") == "progress.stall" and r.get("plan_id") == pid)
                + sum(1 for m, r in rob_events if m == robot and r.get("type") == "follow.stall" and r.get("plan_id") == pid),
                "repairs": sum(1 for r in base if r.get("type") == "repair.decision"
                               and pid in json.dumps(r.get("payload") or {}, default=str)),
            }  # fmt: skip
        g["robots"] = per
    return sorted(out.values(), key=lambda g: g.get("t_sent") or 0)


def chat(mdir: pathlib.Path) -> list[dict]:
    recs, _ = _events(mdir)
    out = []
    for m, _s, r in recs:
        t = str(r.get("type") or "")
        if m == BASE and (
            t.startswith("chat.") or t == "llm.call" or t.startswith("goal.proposed")
        ):
            out.append(
                {
                    "t": r["t_base"],
                    "type": t,
                    "payload": r.get("payload"),
                    "goal_id": r.get("goal_id"),
                }
            )
    return sorted(out, key=lambda x: x["t"])


def stats(mdir: pathlib.Path) -> dict:
    recs, offs = _events(mdir)
    g = goals(mdir)
    poses = _poses(recs)
    link: dict = {}
    llm = {"calls": 0, "in_tok": 0, "out_tok": 0, "cost_usd": 0.0, "latency_ms": []}
    for m, _s, r in recs:
        if m != BASE:
            continue
        p = r.get("payload") or {}
        if r.get("type") == "link.stats":
            link.setdefault(p.get("robot") or r.get("robot"), []).append(
                [round(r["t_base"], 1), p.get("latency_ms"), p.get("rtt_ms"),
                 max((p.get("loss") or {}).values(), default=None) if isinstance(p.get("loss"), dict) else None])  # fmt: skip
        elif r.get("type") == "llm.call":
            llm["calls"] += 1
            llm["in_tok"] += int(p.get("in_tok") or 0)
            llm["out_tok"] += int(p.get("out_tok") or 0)
            llm["cost_usd"] += float(p.get("cost_usd") or 0.0)
            if p.get("latency_ms") is not None:
                llm["latency_ms"].append(p["latency_ms"])
    llm["cost_usd"] = round(llm["cost_usd"], 4)
    lat = sorted(llm.pop("latency_ms"))
    llm["latency_ms_p50"] = lat[len(lat) // 2] if lat else None
    llm["latency_ms_max"] = lat[-1] if lat else None
    per_robot = {}
    for robot, rows in poses.items():
        per_robot[robot] = {
            "driven_m": _driven(rows, -1e18, 1e18),
            "goals": sum(1 for x in g if robot in (x.get("robots") or {})),
            "done": sum(1 for x in g if (x.get("robots") or {}).get(robot, {}).get("result") == "done"),
            "failed": sum(1 for x in g if (x.get("robots") or {}).get(robot, {}).get("result") in ("failed", "plan_failed")),
        }  # fmt: skip
    table = []
    for x in g:
        for robot, v in (x.get("robots") or {}).items():
            table.append(
                {"goal_id": x["goal_id"], "robot": robot, "kind": x.get("kind"), **v}
            )
    return {"goals": table, "robots": per_robot, "llm": llm, "link": link,
            "clock": {r: [[round(t, 1), round(o * 1000, 1)] for t, o in v] for r, v in offs.samples.items()}}  # fmt: skip


def stats_csv(st: dict) -> str:
    cols = ["goal_id", "robot", "kind", "result", "engine", "plan_id", "solve_s", "planned_m", "driven_m",
            "time_to_reach_s", "leases", "stalls", "repairs"]  # fmt: skip
    lines = [",".join(cols)]
    for row in st["goals"]:
        lines.append(
            ",".join(
                "" if row.get(c) is None else str(row.get(c)).replace(",", ";")
                for c in cols
            )
        )
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ replay
def map_nodes(dsg_path: pathlib.Path) -> dict:
    """Objects / places of a spark_dsg JSON (generic: ``nodes[].attributes.position``)."""
    try:
        data = json.loads(dsg_path.read_text())
    except (OSError, ValueError):
        return {"objects": [], "places": [], "source": None}
    nodes = data.get("nodes") if isinstance(data, dict) else None
    objects, places = [], []
    for n in nodes or []:
        a = n.get("attributes") or {}
        pos = a.get("position")
        if not isinstance(pos, (list, tuple)) or len(pos) < 2:
            continue
        layer = n.get("layer")
        if isinstance(layer, (list, tuple)):
            layer = layer[0]
        x, y = round(float(pos[0]), 2), round(float(pos[1]), 2)
        if layer == 2 or str(layer) == "2":
            objects.append([x, y, a.get("name") or a.get("semantic_label")])
        elif layer in (3, "3", 20, "20"):
            places.append([x, y])
    step_o = max(1, len(objects) // MAP_NODES_MAX)
    step_p = max(1, len(places) // MAP_NODES_MAX)
    return {
        "objects": objects[::step_o],
        "places": places[::step_p],
        "source": str(dsg_path),
    }


def replay(mdir: pathlib.Path, prior_dsg: pathlib.Path | None = None) -> dict:
    recs, _ = _events(mdir)
    m = read_json(mdir / "mission.json") or {}
    plans = []
    reloc = []
    stages = []
    for mach, _s, r in recs:
        t, p = r.get("type"), r.get("payload") or {}
        if mach == BASE and t == "plan.solved":
            for robot, acts in (p.get("actions") or {}).items():
                path = [pt for a in acts or [] for pt in (a.get("path") or [])]
                plans.append({"t": r["t_base"], "robot": robot, "plan_id": (p.get("plan_ids") or {}).get(robot)
                              or p.get("plan_id"), "engine": p.get("engine"), "path": path,
                              "len_m": (p.get("path_len_m") or {}).get(robot)})  # fmt: skip
        elif mach == BASE and str(t).startswith("reloc."):
            reloc.append({"t": r["t_base"], "type": t, "robot": p.get("robot") or r.get("robot"),
                          "state": p.get("verdict") or p.get("state") or t.split(".", 1)[1]})  # fmt: skip
        elif mach == BASE and t == "goal.stage" and p.get("robot"):
            stages.append(
                {
                    "t": r["t_base"],
                    "robot": p["robot"],
                    "goal_id": r.get("goal_id"),
                    "to": p.get("to"),
                }
            )
    # the relocalization phase is before the mission: its record (reloc.json)
    seen = {(x["robot"], x["state"], round(x["t"] or 0, 1)) for x in reloc}
    for robot, v in (
        (read_json(mdir / "reloc.json") or {}).get("robots") or {}
    ).items():
        for h in v.get("history") or [{"state": v.get("state"), "t": v.get("t")}]:
            key = (robot, h.get("state"), round(h.get("t") or 0, 1))
            if h.get("state") and key not in seen:
                reloc.append({"t": h.get("t"), "type": f"reloc.{h.get('state')}", "robot": robot,
                              "state": h.get("state")})  # fmt: skip
    changes = []
    for c in iter_jsonl(mdir / "scene_changes.jsonl"):
        d = c.get("data") if isinstance(c.get("data"), dict) else {}
        xyz = d.get("xyz") or d.get("position")
        changes.append({"t": c.get("time"), "seq": c.get("seq"), "op": c.get("op"), "source": c.get("source"),
                        "symbol": d.get("symbol"), "label": d.get("label"),
                        "xy": [round(float(xyz[0]), 2), round(float(xyz[1]), 2)] if isinstance(xyz, list) and len(xyz) >= 2 else None})  # fmt: skip
    dsg = mdir / "dsg_end.json"
    base_map = (
        map_nodes(dsg)
        if dsg.exists()
        else (map_nodes(prior_dsg) if prior_dsg else {"objects": [], "places": []})
    )
    poses = _poses(recs)
    ts = [x[0] for v in poses.values() for x in v] + [x["t"] for x in plans]
    return {
        "mission_id": m.get("mission_id") or mdir.name,
        "t_start": m.get("t_start"),
        "t_end": m.get("t_end"),
        "t_range": [min(ts), max(ts)] if ts else [m.get("t_start"), m.get("t_end")],
        "poses": poses,
        "plans": sorted(plans, key=lambda x: x["t"]),
        "changes": sorted(changes, key=lambda x: (x["t"] or 0)),
        "reloc": sorted(reloc, key=lambda x: x["t"] or 0),
        "stages": sorted(stages, key=lambda x: x["t"]),
        "map": base_map,
    }


# ------------------------------------------------------------------ export
def export(
    mdir: pathlib.Path, out_dir: pathlib.Path, include_bags: bool = False
) -> pathlib.Path:
    """``<out_dir>/<mission_id>.tar.zst`` (images in; bags only with ``include_bags``;
    ``index.sqlite`` left out: it is rebuilt from the events).  Raises
    PermissionError while ``report.json`` says the export is blocked."""
    rep = read_json(mdir / "report.json") or {}
    if rep.get("export_blocked"):
        raise PermissionError("the mission check found a secret: export blocked")
    out_dir.mkdir(parents=True, exist_ok=True)

    def keep(ti: tarfile.TarInfo):
        parts = pathlib.PurePosixPath(ti.name).parts
        if parts and parts[-1] in ("index.sqlite",) or ti.name.endswith(".tmp"):
            return None
        if not include_bags and any(p.startswith("mission_bag") for p in parts):
            return None
        return ti

    zstd = shutil.which("zstd")
    stem = out_dir / f"{mdir.name}"
    tar_path = stem.with_suffix(".tar")
    with tarfile.open(tar_path, "w") as tf:
        tf.add(mdir, arcname=mdir.name, filter=keep)
    if zstd:
        out = stem.with_suffix(".tar.zst")
        subprocess.run(
            [zstd, "-q", "-f", "--rm", "-o", str(out), str(tar_path)], check=True
        )
        return out
    import gzip

    out = stem.with_suffix(".tar.gz")
    with open(tar_path, "rb") as src, gzip.open(out, "wb") as dst:
        shutil.copyfileobj(src, dst)
    tar_path.unlink()
    return out


def safe_file(mdir: pathlib.Path, rel: str) -> pathlib.Path | None:
    """A file inside the mission folder (or the base dir's ``missions/<id>/``
    form a base event uses); None for anything outside."""
    for cand in (mdir / rel, mdir.parent.parent / rel):
        try:
            p = cand.resolve()
        except OSError:
            continue
        if p.is_file() and (mdir.resolve() in p.parents):
            return p
    return None
