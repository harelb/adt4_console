"""``manifest.json`` of a mission folder (plan §5): what it holds and where it came from.

Mission meta, the expected machines (the session's robots and base station),
per machine its runs, files ``{path, size, sha256, t_first, t_last, n_events}``,
bags and image stores (referenced), collect status; clock offsets per robot;
goals and outcomes; image refs ``{path, exists, sha256}``; the ``_SceneChange``
seq range and map state; git SHAs from each ``run_info``; LLM totals.
Never the environment.
"""

from __future__ import annotations

import hashlib
import pathlib
import time

from . import BASE, LIVE_FILE, Offsets, all_events, machines, read_json, write_json

VERSION = 1


def sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def session_of(mdir: pathlib.Path, mission: dict) -> dict:
    sid = mission.get("session_id")
    if not sid:
        return {}
    return read_json(mdir.parent.parent / "sessions" / sid / "session.json") or {}


def expected_machines(mission: dict, session: dict) -> list[str]:
    """``base`` + the session's (else the mission's) robots."""
    robots = list(session.get("robots") or mission.get("robots") or [])
    return [BASE] + [r for r in robots if r != BASE]


def _files(d: pathlib.Path, root: pathlib.Path, skip=("index.sqlite",)) -> list[dict]:
    out = []
    for p in sorted(d.rglob("*")) if d.is_dir() else []:
        if p.is_file() and p.name not in skip and not p.name.endswith(".tmp"):
            out.append(
                {
                    "path": str(p.relative_to(root)),
                    "size": p.stat().st_size,
                    "sha256": sha256(p),
                }
            )
    return out


def build(mdir, write: bool = True) -> dict:
    mdir = pathlib.Path(mdir)
    mission = read_json(mdir / "mission.json") or {}
    session = session_of(mdir, mission)
    collect = read_json(mdir / "collect.json") or {}
    expected = expected_machines(mission, session)
    present = machines(mdir)
    recs = list(all_events(mdir))
    offsets = Offsets.from_events(r for m, _, r in recs if m == BASE)
    per = {}
    for m in sorted(set(expected) | set(present), key=lambda n: (n != BASE, n)):
        d = mdir / m
        sl = read_json(d / "slice.json") or {}
        status = (collect.get("machines") or {}).get(m, {}).get("state")
        if status is None:
            status = (
                "collected"
                if sl
                else ("live_only" if (d / LIVE_FILE).exists() else "missing")
            )
        files = list(sl.get("files") or [])
        if (d / LIVE_FILE).exists():
            files.append(
                {
                    "path": LIVE_FILE,
                    "size": (d / LIVE_FILE).stat().st_size,
                    "sha256": sha256(d / LIVE_FILE),
                }
            )
        runs = []
        for r in sl.get("runs") or []:
            info = r.get("run_info") or {}
            runs.append({
                "run_id": r.get("run_id"), "path": r.get("path"), "session_id": info.get("session_id"),
                "machine": info.get("machine"), "robot": info.get("robot"), "platform": info.get("platform"),
                "session": info.get("session"), "config": info.get("config"), "t_start": info.get("t_start"),
                "git": info.get("git"), "git_commit": info.get("git_commit"),
                "bags": [{k: b.get(k) for k in ("path", "size", "metadata_sha256", "t_start", "t_end", "open",
                                                "message_count")} for b in r.get("bags") or []],
                "image_store": r.get("image_store"), "ros_log": r.get("ros_log"),
            })  # fmt: skip
        per[m] = {"expected": m in expected, "collect": status, "runs": runs, "files": files,
                  "n_events": sum(f.get("n_events") or 0 for f in files), "sliced": sl.get("t_sliced"),
                  "machine": sl.get("machine")}  # fmt: skip
    goals: dict = {}
    llm = {"calls": 0, "in_tok": 0, "out_tok": 0, "cost_usd": 0.0, "models": {}}
    for m, _src, r in recs:
        t, p = r.get("type"), r.get("payload") or {}
        if t == "goal.sent":
            gid = r.get("goal_id") or p.get("track_id")
            if gid:
                goals.setdefault(gid, {"goal_id": gid, "outcomes": {}})
                goals[gid].update(t_sent=r.get("t_wall"), robot=p.get("robot"), kind=p.get("kind"),
                                  robots=p.get("robots"))  # fmt: skip
        elif t == "goal.outcome" and r.get("goal_id"):
            g = goals.setdefault(
                r["goal_id"], {"goal_id": r["goal_id"], "outcomes": {}}
            )
            g["outcomes"][p.get("robot") or ""] = {
                "result": p.get("result"),
                "duration_s": p.get("duration_s"),
            }
        elif t == "llm.call":
            llm["calls"] += 1
            llm["in_tok"] += int(p.get("in_tok") or 0)
            llm["out_tok"] += int(p.get("out_tok") or 0)
            llm["cost_usd"] += float(p.get("cost_usd") or 0.0)
            mo = p.get("resp_model") or p.get("model") or "?"
            llm["models"][mo] = llm["models"].get(mo, 0) + 1
    llm["cost_usd"] = round(llm["cost_usd"], 4)
    clock = {}
    for robot, v in offsets.samples.items():
        vals = [o for _, o in v]
        clock[robot] = {"n": len(vals), "min_s": min(vals), "max_s": max(vals),
                        "mean_s": sum(vals) / len(vals), "max_abs_s": max(abs(x) for x in vals)}  # fmt: skip
    images = []
    for p in sorted((mdir / "images").rglob("*")) if (mdir / "images").is_dir() else []:
        if p.is_file():
            images.append({"path": str(p.relative_to(mdir)), "exists": True, "size": p.stat().st_size,
                           "sha256": sha256(p)})  # fmt: skip
    git = {m: [{"run_id": r["run_id"], "git_commit": r.get("git_commit"),
                "submodules": ((r.get("git") or {}).get("submodules"))} for r in v["runs"]]
           for m, v in per.items()}  # fmt: skip
    top = []
    for name in ("mission.json", "reloc.json", "scene_changes.jsonl", "map_state.json", "dsg_end.json",
                 "collect.json"):  # fmt: skip
        p = mdir / name
        if p.exists():
            top.append({"path": name, "size": p.stat().st_size, "sha256": sha256(p)})
    plans = _files(mdir / "plans", mdir)
    man = {
        "v": VERSION,
        "mission_id": mission.get("mission_id") or mdir.name,
        "name": mission.get("name"),
        "session_id": mission.get("session_id"),
        "experiment": mission.get("experiment"),
        "t_start": mission.get("t_start"),
        "t_end": mission.get("t_end"),
        "duration_s": round(float(mission["t_end"]) - float(mission["t_start"]), 1)
        if mission.get("t_end") and mission.get("t_start")
        else None,
        "start_reason": mission.get("start_reason"),
        "end_reason": mission.get("end_reason"),
        "robots": mission.get("robots"),
        "run_ids": mission.get("run_ids"),
        "base_station": session.get("base_station"),
        "expected_machines": expected,
        "machines": per,
        "clock_offsets": clock,
        "goals": list(goals.values()),
        "images": images,
        "plans": plans,
        "scene_changes": read_json(mdir / "map_state.json"),
        "git": git,
        "llm": llm,
        "files": top,
        "t_built": time.time(),
    }
    if write:
        write_json(mdir / "manifest.json", man)
    return man
