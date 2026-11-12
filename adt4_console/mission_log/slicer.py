#!/usr/bin/env python3
"""``adt4-mission-slice``: cut one mission out of a machine's runs (plan §5).

Standalone: Python 3.8+ standard library only, so the collector can run it on a
robot over ssh without anything installed there (it sends this file's source).

    adt4-mission-slice --exp ~/adt4_output/<experiment> [--exp ...]
        --mission <mission_id> --t0 <unix s> --t1 <unix s> --out <dir>
        [--session <session_id>] [--pad 60] [--offset <s>] [--root-events]

* Runs: every ``<exp>/runs/<run_id>/`` (and a flat legacy ``<exp>`` with a
  ``run_info.json``) whose ``session_id`` is the mission's session, or that
  overlaps the window when the session is unknown.
* Events: each run's ``events/<source>[.<n>].jsonl`` lines with ``t_wall`` in
  ``[t0 - pad, t1 + pad]`` (``--offset``: this machine's clock minus the base's,
  so the window is moved into this clock) or stamped with the mission id, merged
  per source into ``<out>/events/<source>.jsonl`` (ordered by ``t_wall``).
  ``--root-events`` also takes ``<exp>/events`` (the console's own log).
* Artifacts, per run under ``<out>/runs/<run_id>/``: ``run_info.json``,
  ``spot_executor/{str_log,lease_log}.txt``, hydra / hydra_multi
  ``loop_closures.csv``, ``*trajectory*.csv``, ``timing_stats.csv``, the
  ``omniplanner/`` dumps written in the window, the ``grasp_episodes/`` the
  window's events name (or that were written in it), ``mission_bag_topics.txt``.
* Referenced only (never copied): ``mission_bag/`` (size, ``metadata.yaml``
  sha256 / time range / per-topic counts, or ``open`` while still recording),
  ``image_store/`` (size, index sha256), ``ros_log/`` (size).
* ``<out>/slice.json``: what was found, every written file with size / sha256
  and, for events, ``t_first`` / ``t_last`` / ``n_events``.

Never reads or writes the process environment (no secrets in the output).
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import socket
import sys
import time

VERSION = 1
PAD_S = 60.0
SMALL_FILES = (
    "spot_executor/str_log.txt",
    "spot_executor/lease_log.txt",
    "mission_bag_topics.txt",
    "hydra/timing_stats.csv",
    "hydra_multi/timing_stats.csv",
    "hydra/backend/loop_closures.csv",
    "hydra_multi/backend/loop_closures.csv",
    "launch.json",
)
TRAJ_GLOBS = ("hydra/backend/*trajectory*.csv", "hydra_multi/backend/*trajectory*.csv")
MAX_COPY_BYTES = (
    50 * 1024 * 1024
)  # a "small" artifact bigger than this is referenced only
ROTATED = re.compile(r"^(?P<src>.+?)(?:\.(?P<n>\d+))?\.jsonl$")


def sha256(path: str, limit: int | None = None) -> str:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
            n += len(b)
            if limit is not None and n >= limit:
                break
    return h.hexdigest()


def dir_size(path: str) -> int:
    total = 0
    for dp, _, fns in os.walk(path):
        for fn in fns:
            try:
                total += os.path.getsize(os.path.join(dp, fn))
            except OSError:
                pass
    return total


def read_json(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def find_runs(exp: str) -> list[str]:
    exp = os.path.expanduser(exp)
    runs = sorted(
        d for d in glob.glob(os.path.join(exp, "runs", "*")) if os.path.isdir(d)
    )
    if not runs and os.path.exists(os.path.join(exp, "run_info.json")):
        runs = [exp]  # flat legacy layout
    return runs


def run_last_t(run: str) -> float:
    """Newest mtime of the run's events (else the run dir)."""
    ts = [
        os.path.getmtime(p) for p in glob.glob(os.path.join(run, "events", "*.jsonl"))
    ]
    try:
        ts.append(os.path.getmtime(run))
    except OSError:
        pass
    return max(ts) if ts else 0.0


def select_runs(
    exps, session: str | None, t0: float, t1: float
) -> list[tuple[str, dict]]:
    out = []
    for exp in exps:
        for run in find_runs(exp):
            info = read_json(os.path.join(run, "run_info.json")) or {}
            sid = info.get("session_id")
            if session and sid:
                if sid != session:
                    continue
            else:
                start = float(info.get("t_start") or 0.0)
                if start and start > t1:
                    continue
                if run_last_t(run) < t0:
                    continue
            info.setdefault("run_id", os.path.basename(run.rstrip("/")))
            out.append((run, info))
    return out


def event_files(events_dir: str) -> dict[str, list[str]]:
    """source -> its files, rotated ones (``<src>.<n>.jsonl``) first."""
    by = {}
    for p in glob.glob(os.path.join(events_dir, "*.jsonl")):
        m = ROTATED.match(os.path.basename(p))
        if not m:
            continue
        by.setdefault(m.group("src"), []).append((int(m.group("n") or 10**9), p))
    return {s: [p for _, p in sorted(v)] for s, v in by.items()}


def slice_events(
    files, lo: float, hi: float, mission: str | None
) -> list[tuple[float, int, str]]:
    rows = []
    for p in files:
        try:
            f = open(p, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    t = float(rec.get("t_wall") or 0.0)
                except (ValueError, TypeError, AttributeError):
                    continue
                if lo <= t <= hi or (mission and rec.get("mission_id") == mission):
                    rows.append((t, int(rec.get("seq") or 0), line))
    return rows


def copy_small(src: str, dst: str) -> dict | None:
    try:
        size = os.path.getsize(src)
    except OSError:
        return None
    if size > MAX_COPY_BYTES:
        return {"path": dst, "size": size, "copied": False, "reason": "too big"}
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return {"path": dst, "size": size, "copied": True}


def parse_bag_metadata(text: str) -> dict:
    """The bits of a rosbag2 ``metadata.yaml`` the check needs (no yaml lib)."""
    out: dict = {"topics": {}}
    m = re.search(r"duration:\s*\n\s*nanoseconds:\s*(\d+)", text)
    if m:
        out["duration_s"] = int(m.group(1)) / 1e9
    m = re.search(r"starting_time:\s*\n\s*nanoseconds_since_epoch:\s*(\d+)", text)
    if m:
        out["t_start"] = int(m.group(1)) / 1e9
    m = re.search(r"^\s{2}message_count:\s*(\d+)", text, re.M)
    if m:
        out["message_count"] = int(m.group(1))
    for m in re.finditer(r"name:\s*(\S+)\n(?:.*\n)*?\s*message_count:\s*(\d+)", text):
        name = m.group(1)
        if name.startswith("/"):
            out["topics"][name] = int(m.group(2))
    if "t_start" in out and "duration_s" in out:
        out["t_end"] = out["t_start"] + out["duration_s"]
    return out


def bag_info(path: str) -> dict:
    meta = os.path.join(path, "metadata.yaml")
    files = sorted(
        glob.glob(os.path.join(path, "*.mcap")) + glob.glob(os.path.join(path, "*.db3"))
    )
    info = {
        "path": path,
        "size": dir_size(path),
        "files": [
            {
                "name": os.path.basename(f),
                "size": os.path.getsize(f),
                "mtime": os.path.getmtime(f),
            }
            for f in files
        ],  # fmt: skip
    }
    if os.path.exists(meta):
        with open(meta, encoding="utf-8", errors="replace") as f:
            text = f.read()
        info.update(parse_bag_metadata(text), metadata_sha256=sha256(meta), open=False)
    else:
        info["open"] = bool(files)  # still recording (metadata.yaml comes at close)
        if files:
            info["t_first_file"] = min(os.path.getctime(f) for f in files)
            info["t_last_write"] = max(os.path.getmtime(f) for f in files)
    return info


def store_info(path: str) -> dict:
    info = {"path": path, "size": dir_size(path)}
    idx = sorted(glob.glob(os.path.join(path, "*.db")) + glob.glob(os.path.join(path, "*.sqlite"))
                 + glob.glob(os.path.join(path, "index*")))  # fmt: skip
    if idx:
        info["index"] = os.path.basename(idx[0])
        info["index_sha256"] = sha256(idx[0], limit=64 * 1024 * 1024)
    return info


GRASP_TS = re.compile(r"^(\d{8}[-_]?\d{6})")


def grasp_dirs(run: str, lo: float, hi: float, named: set) -> list[str]:
    out = []
    for d in sorted(
        glob.glob(os.path.join(run, "spot_executor", "grasp_episodes", "*"))
    ):
        if not os.path.isdir(d):
            continue
        base = os.path.basename(d)
        if base in named or d in named:
            out.append(d)
            continue
        try:
            t = os.path.getmtime(d)
        except OSError:
            continue
        if lo <= t <= hi:
            out.append(d)
    return out


def file_entry(path: str, rel_to: str, events: bool = False) -> dict:
    e = {
        "path": os.path.relpath(path, rel_to),
        "size": os.path.getsize(path),
        "sha256": sha256(path),
    }
    if events:
        n, first, last = 0, None, None
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    t = float(json.loads(line).get("t_wall"))
                except (ValueError, TypeError, AttributeError):
                    continue
                n += 1
                first = t if first is None else min(first, t)
                last = t if last is None else max(last, t)
        e.update(n_events=n, t_first=first, t_last=last)
    return e


def run_slice(exps, mission, t0, t1, out, session=None, pad=PAD_S, offset=0.0, root_events=False,
              machine=None) -> dict:  # fmt: skip
    """Write the slice; returns ``slice.json``'s content."""
    out = os.path.expanduser(out)
    lo, hi = t0 - pad + offset, t1 + pad + offset
    os.makedirs(out, exist_ok=True)
    runs = select_runs(exps, session, lo, hi)
    by_source: dict[str, list] = {}
    referenced: set = set()
    run_rows = []
    sources = []
    for run, info in runs:
        sources.append((os.path.join(run, "events"), info["run_id"]))
    if root_events:
        for exp in exps:
            sources.append((os.path.join(os.path.expanduser(exp), "events"), None))
    for events_dir, _rid in sources:
        for src, files in event_files(events_dir).items():
            rows = slice_events(files, lo, hi, mission)
            by_source.setdefault(src, []).extend(rows)
            for _, _, line in rows:
                if '"grasp.episode"' in line:
                    try:
                        d = (json.loads(line).get("payload") or {}).get("dir")
                    except ValueError:
                        d = None
                    if d:
                        referenced.add(d)
                        referenced.add(os.path.basename(str(d).rstrip("/")))
    ev_dir = os.path.join(out, "events")
    if os.path.isdir(ev_dir):
        shutil.rmtree(ev_dir)  # a re-slice replaces the previous one
    os.makedirs(ev_dir, exist_ok=True)
    files = []
    for src, rows in sorted(by_source.items()):
        if not rows:
            continue
        rows.sort(key=lambda r: (r[0], r[1]))
        seen = set()
        p = os.path.join(ev_dir, f"{src}.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            for _, _, line in rows:
                if line in seen:
                    continue  # the same line in a run and a root dir
                seen.add(line)
                f.write(line + "\n")
        files.append(file_entry(p, out, events=True))
    for run, info in runs:
        rid = info["run_id"]
        dst = os.path.join(out, "runs", rid)
        rr = {
            "run_id": rid,
            "path": run,
            "run_info": info,
            "copied": [],
            "bags": [],
            "image_store": None,
        }
        if os.path.exists(os.path.join(run, "run_info.json")):
            copy_small(
                os.path.join(run, "run_info.json"), os.path.join(dst, "run_info.json")
            )
        for rel in SMALL_FILES:
            c = copy_small(os.path.join(run, rel), os.path.join(dst, rel))
            if c:
                rr["copied"].append(rel)
        for pat in TRAJ_GLOBS:
            for p in glob.glob(os.path.join(run, pat)):
                rel = os.path.relpath(p, run)
                if copy_small(p, os.path.join(dst, rel)):
                    rr["copied"].append(rel)
        for p in glob.glob(os.path.join(run, "omniplanner", "*")):
            if os.path.isfile(p) and lo <= os.path.getmtime(p) <= hi:
                rel = os.path.relpath(p, run)
                if copy_small(p, os.path.join(dst, rel)):
                    rr["copied"].append(rel)
        for d in grasp_dirs(run, lo, hi, referenced):
            rel = os.path.relpath(d, run)
            tgt = os.path.join(dst, rel)
            if os.path.isdir(tgt):
                shutil.rmtree(tgt)
            shutil.copytree(d, tgt)
            rr["copied"].append(rel + "/")
        for b in sorted(glob.glob(os.path.join(run, "mission_bag*"))):
            if os.path.isdir(b):
                rr["bags"].append(bag_info(b))
        for b in sorted(glob.glob(os.path.join(run, "recorded_data*"))):
            if os.path.isdir(b):
                rr.setdefault("recordings", []).append({"path": b, "size": dir_size(b)})
        if os.path.isdir(os.path.join(run, "image_store")):
            rr["image_store"] = store_info(os.path.join(run, "image_store"))
        if os.path.isdir(os.path.join(run, "ros_log")):
            rr["ros_log"] = {
                "path": os.path.join(run, "ros_log"),
                "size": dir_size(os.path.join(run, "ros_log")),
            }
        run_rows.append(rr)
    for dp, _, fns in os.walk(os.path.join(out, "runs")):
        for fn in fns:
            files.append(file_entry(os.path.join(dp, fn), out))
    result = {
        "v": VERSION,
        "mission_id": mission,
        "session_id": session,
        "machine": machine or socket.gethostname(),
        "t0": t0,
        "t1": t1,
        "pad_s": pad,
        "offset_s": offset,
        "window": [lo, hi],
        "experiments": [os.path.expanduser(e) for e in exps],
        "runs": run_rows,
        "files": files,
        "n_events": sum(f.get("n_events", 0) for f in files),
        "t_sliced": time.time(),
    }
    with open(os.path.join(out, "slice.json"), "w") as f:
        json.dump(result, f, indent=1, default=str)
    return result


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="adt4-mission-slice", description=__doc__.split("\n\n")[0]
    )
    p.add_argument(
        "--exp", action="append", required=True, help="experiment dir (repeatable)"
    )
    p.add_argument("--mission", required=True)
    p.add_argument("--session", default=None)
    p.add_argument("--t0", type=float, required=True)
    p.add_argument("--t1", type=float, required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--pad", type=float, default=PAD_S)
    p.add_argument(
        "--offset", type=float, default=0.0, help="this clock minus the base clock (s)"
    )
    p.add_argument(
        "--root-events", action="store_true", help="also <exp>/events (console log)"
    )
    p.add_argument("--machine", default=None)
    a = p.parse_args(argv)
    r = run_slice(
        a.exp,
        a.mission,
        a.t0,
        a.t1,
        a.out,
        a.session,
        a.pad,
        a.offset,
        a.root_events,
        a.machine,
    )
    # one line the collector parses (the last line of stdout)
    print(json.dumps({"ok": True, "out": os.path.expanduser(a.out), "runs": [x["run_id"] for x in r["runs"]],
                      "n_events": r["n_events"], "files": len(r["files"])}))  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
