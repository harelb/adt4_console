"""``adt4-mission check``: verify a mission folder (plan §6) -> ``report.json``.

Every check is PASS / WARN / FAIL (or SKIP when it does not apply), with the
first offending event for a failure.  The overall status is the worst one; the
CLI exits 1 on FAIL.  A secret found anywhere in the folder FAILs the check and
blocks the export.

Times: robot events carry the robot's ``t_wall``; they are mapped to the base
clock with the base's ``clock.offset`` samples (:class:`Offsets`).

Goals <-> plans: omniplanner messages carry no goal id; the console's goal
tracker joins them: its ``goal.stage`` events name the ``plan_id`` it attached
to each goal (and robot), which is what the plan / action / lease checks follow.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import time

from . import (
    BASE,
    LIVE_FILE,
    PAD_S,
    Offsets,
    all_events,
    iter_jsonl,
    read_json,
    write_json,
)
from .manifest import expected_machines, session_of

PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"
ORDER = {SKIP: 0, PASS: 1, WARN: 2, FAIL: 3}
CLOCK_WARN_S = 0.25
CLOCK_FAIL_S = 2.0
GAP_WARN_S = 30.0
GAP_FAIL_S = 120.0
LEASE_MATCH_S = 60.0
BAG_TOL_S = 60.0
RELOC_OK = ("relocalized", "not_required")
AUTO_END = ("idle", "auto", "auto_close")
LEASE_BASE_STAGES = ("lease_wait", "lease_unknown", "paused", "stopped", "preempted")
HEARTBEAT_SOURCES = {"robot": ("fleetlink_agent",), "base": ("console",)}
SECRET_PATTERNS = [
    ("openai_key", re.compile(rb"sk-(?:proj-|ant-)?[A-Za-z0-9_\-]{20,}")),
    ("aws_key", re.compile(rb"AKIA[0-9A-Z]{16}")),
    ("private_key", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer", re.compile(rb"Bearer\s+[A-Za-z0-9._\-]{20,}")),
]
SECRET_ENV = re.compile(r"(API_?KEY|PASSWORD|PASSWD|TOKEN|SECRET)", re.I)
SCAN_MAX_BYTES = 64 * 1024 * 1024


class Report:
    def __init__(self):
        self.checks: list[dict] = []

    def add(self, cid, title, status, detail="", first=None, items=None) -> dict:
        c = {"id": cid, "title": title, "status": status, "detail": detail}
        if first is not None:
            c["first"] = _brief(first)
        if items:
            c["items"] = items[:50]
            c["n_items"] = len(items)
        self.checks.append(c)
        return c

    @property
    def status(self) -> str:
        worst = PASS
        for c in self.checks:
            if ORDER[c["status"]] > ORDER[worst]:
                worst = c["status"]
        return worst


def _brief(rec) -> dict:
    if not isinstance(rec, dict) or "type" not in rec:
        return rec
    return {k: rec.get(k) for k in ("t_wall", "machine", "robot", "source", "type", "seq", "goal_id", "plan_id",
                                    "mission_id", "payload")}  # fmt: skip


# ------------------------------------------------------------------ loading
class Mission:
    def __init__(self, mdir):
        self.dir = pathlib.Path(mdir)
        self.mission = read_json(self.dir / "mission.json") or {}
        self.session = session_of(self.dir, self.mission)
        self.reloc = read_json(self.dir / "reloc.json") or {}
        self.collect = read_json(self.dir / "collect.json") or {}
        self.expected = expected_machines(self.mission, self.session)
        self.robots = [m for m in self.expected if m != BASE]
        self.by_machine: dict[str, list] = {}
        for m, src, rec in all_events(self.dir):
            rec = dict(rec)
            rec["_src"] = src
            self.by_machine.setdefault(m, []).append(rec)
        for v in self.by_machine.values():
            v.sort(key=lambda r: float(r.get("t_wall") or 0.0))
        self.base = self.by_machine.get(BASE, [])
        self.offsets = Offsets.from_events(self.base)
        self.t0 = float(self.mission.get("t_start") or 0.0)
        self.t1 = float(self.mission.get("t_end") or time.time())
        self.mid = self.mission.get("mission_id") or self.dir.name

    def base_t(self, machine: str, rec: dict) -> float:
        t = float(rec.get("t_wall") or 0.0)
        return t if machine == BASE else self.offsets.to_base(machine, t)

    def in_mission(self, machine, rec, pad=0.0) -> bool:
        t = self.base_t(machine, rec)
        return self.t0 - pad <= t <= self.t1 + pad or rec.get("mission_id") == self.mid

    def of(self, machine: str, *types, src=None):
        for r in self.by_machine.get(machine, []):
            if (not types or r.get("type") in types) and (
                src is None or r["_src"] == src
            ):
                yield r


# ------------------------------------------------------------------ checks
def check_machines(M: Mission, R: Report):
    items, worst = [], PASS
    for m in M.expected:
        d = M.dir / m
        sl = read_json(d / "slice.json")
        state = ((M.collect.get("machines") or {}).get(m) or {}).get("state")
        if sl:
            runs = [
                r
                for r in sl.get("runs") or []
                if r.get("run_info") and r["run_info"].get("run_id")
            ]
            has_events = any(
                f["path"].startswith("events/") for f in sl.get("files") or []
            )
            if runs and has_events:
                items.append(
                    {"machine": m, "status": PASS, "runs": [r["run_id"] for r in runs]}
                )
                continue
            st = WARN if has_events else FAIL
            items.append(
                {
                    "machine": m,
                    "status": st,
                    "why": "no run_info" if has_events else "no events",
                }
            )
        elif (d / LIVE_FILE).exists():
            st = WARN
            items.append(
                {
                    "machine": m,
                    "status": WARN,
                    "why": f"live_only ({state or 'not collected'})",
                }
            )
        else:
            st = FAIL
            items.append(
                {
                    "machine": m,
                    "status": FAIL,
                    "why": f"missing ({state or 'not collected'})",
                }
            )
        worst = max(worst, st, key=ORDER.get)
    R.add("machines", "Every expected machine has run_info and events", worst,
          ", ".join(f"{i['machine']}: {i['status']}" for i in items), items=items)  # fmt: skip


def check_mission_edges(M: Mission, R: Report):
    starts = [
        r
        for r in M.of(BASE, "mission.start")
        if (r.get("payload") or {}).get("mission_id") == M.mid
    ]
    ends = [
        r
        for r in M.of(BASE, "mission.end")
        if (r.get("payload") or {}).get("mission_id") == M.mid
    ]
    reason = M.mission.get("end_reason")
    if not starts:
        return R.add(
            "mission_edges",
            "Mission start / end present",
            FAIL,
            "no mission.start event",
        )
    if not ends or not M.mission.get("t_end") or not reason:
        return R.add("mission_edges", "Mission start / end present", FAIL,
                     "no mission.end (console crashed or the mission is still open)", first=starts[0])  # fmt: skip
    st = WARN if reason in AUTO_END else PASS
    R.add(
        "mission_edges",
        "Mission start / end present",
        st,
        f"start {M.mission.get('start_reason')}, end {reason}",
    )


def check_reloc(M: Mission, R: Report):
    robots = (M.reloc.get("robots") or {}) if M.reloc else {}
    if not M.session:
        return R.add("reloc", "Relocalization record before the start", WARN,
                     "no console session (mission without a Launch): nothing to check")  # fmt: skip
    items, worst = [], PASS
    for r in M.robots:
        v = robots.get(r)
        if not v:
            items.append({"robot": r, "status": FAIL, "state": None})
            worst = FAIL
            continue
        s = v.get("state")
        st = PASS if s in RELOC_OK else WARN if s == "overridden" else FAIL
        items.append({"robot": r, "status": st, "state": s, "reason": v.get("reason")})
        worst = max(worst, st, key=ORDER.get)
    degraded = [x for x in M.of(BASE, "reloc.degraded") if M.in_mission(BASE, x)]
    if degraded and worst == PASS:
        worst = WARN
    R.add("reloc", "Relocalization record before the start", worst,
          "; ".join(f"{i['robot']}: {i['state']}" for i in items)
          + (f"; {len(degraded)} reloc.degraded during the mission" if degraded else ""),
          first=degraded[0] if degraded else None, items=items)  # fmt: skip


def _topic_patterns(path: pathlib.Path, robot: str) -> list[tuple[str, bool]]:
    out = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        name, _, comment = s.partition("#")
        name = (
            name.strip()
            .replace("${ADT4_ROBOT_NAME}", robot)
            .replace("$ADT4_ROBOT_NAME", robot)
        )
        if name:
            out.append((name, comment.strip().lower().startswith("optional")))
    return out


def _match(pattern: str, topic: str) -> bool:
    if "*" not in pattern:
        return pattern == topic
    rx = "^" + re.escape(pattern).replace(r"\*", "[^/]+") + "$"
    return re.match(rx, topic) is not None


def check_bags(M: Mission, R: Report):
    items, worst = [], PASS
    for m in M.expected:
        sl = read_json(M.dir / m / "slice.json")
        if not sl:
            items.append({"machine": m, "status": FAIL, "why": "not collected"})
            worst = FAIL
            continue
        bags = [(r, b) for r in sl.get("runs") or [] for b in r.get("bags") or []]
        if not bags:
            items.append({"machine": m, "status": FAIL, "why": "no mission_bag"})
            worst = FAIL
            continue
        off = 0.0 if m == BASE else M.offsets.at(m, M.t0)
        closed = [
            (r, b)
            for r, b in bags
            if not b.get("open") and b.get("t_start") is not None
        ]
        opened = [(r, b) for r, b in bags if b.get("open")]
        covered = any(b["t_start"] - off <= M.t0 + BAG_TOL_S and b.get("t_end", 0) - off >= M.t1 - BAG_TOL_S
                      for _, b in closed)  # fmt: skip
        if covered:
            r, b = next((r, b) for r, b in closed
                        if b["t_start"] - off <= M.t0 + BAG_TOL_S and b.get("t_end", 0) - off >= M.t1 - BAG_TOL_S)  # fmt: skip
            robot_name = (r.get("run_info") or {}).get("robot") or m
            pats = _topic_patterns(
                M.dir / m / "runs" / r["run_id"] / "mission_bag_topics.txt", robot_name
            )
            topics = b.get("topics") or {}
            silent = [
                p
                for p, opt in pats
                if not opt and sum(c for t, c in topics.items() if _match(p, t)) == 0
            ]
            opt_silent = [
                p
                for p, opt in pats
                if opt and sum(c for t, c in topics.items() if _match(p, t)) == 0
            ]
            st = WARN if silent else PASS
            items.append({"machine": m, "status": st, "bag": b.get("path"), "messages": b.get("message_count"),
                          "silent_required": silent, "silent_optional": len(opt_silent)})  # fmt: skip
        elif opened and any((b.get("t_first_file") or 0) - off <= M.t0 + BAG_TOL_S
                            and (b.get("t_last_write") or 0) - off >= M.t1 - BAG_TOL_S for _, b in opened):  # fmt: skip
            st = WARN
            items.append({"machine": m, "status": WARN, "why": "still recording (no metadata.yaml yet): re-collect "
                                                                "after Stop & save to check the topics"})  # fmt: skip
        else:
            st = FAIL
            items.append({"machine": m, "status": FAIL, "why": "no bag covers the mission",
                          "bags": [{k: b.get(k) for k in ("path", "t_start", "t_end", "open")} for _, b in bags]})  # fmt: skip
        worst = max(worst, st, key=ORDER.get)
    R.add("bags", "Every machine has a mission bag covering the mission", worst,
          "; ".join(f"{i['machine']}: {i['status']}" + (f" ({len(i['silent_required'])} required topics silent)"
                                                         if i.get("silent_required") else "")
                    for i in items), items=items)  # fmt: skip


def goals(M: Mission) -> dict:
    """goal_id -> {sent, robots (scoped), outcomes {robot|"": rec}, plan_ids {robot: pid}}."""
    out: dict = {}
    for r in M.base:
        t, gid = r.get("type"), r.get("goal_id")
        if not gid:
            continue
        if t == "goal.sent" and M.in_mission(BASE, r, pad=1.0):
            g = out.setdefault(
                gid, {"robots": set(), "outcomes": {}, "plan_ids": {}, "stages": []}
            )
            g["sent"] = r
            p = r.get("payload") or {}
            for x in [p.get("robot"), *(p.get("robots") or [])]:
                if x:
                    g["robots"].add(x)
    for r in M.base:
        gid = r.get("goal_id")
        if gid not in out:
            continue
        g, p = out[gid], r.get("payload") or {}
        if r.get("type") == "goal.stage":
            g["stages"].append(r)
            if p.get("robot"):
                g["robots"].add(p["robot"])
                if p.get("plan_id"):
                    g["plan_ids"][p["robot"]] = p["plan_id"]
        elif r.get("type") == "goal.outcome":
            g["outcomes"][p.get("robot") or ""] = r
    return out


def check_goals(M: Mission, R: Report, G: dict):
    if not G:
        return R.add(
            "goal_outcomes",
            "Every goal sent has an outcome per scoped robot",
            PASS,
            "no goals sent",
        )
    missing, noplan = [], []
    for gid, g in G.items():
        for robot in sorted(g["robots"]) or [""]:
            o = g["outcomes"].get(robot) or g["outcomes"].get("")
            if o is None:
                missing.append({"goal_id": gid, "robot": robot})
            elif (o.get("payload") or {}).get("result") == "no_plan":
                noplan.append({"goal_id": gid, "robot": robot})
    st = FAIL if missing else WARN if noplan else PASS
    first = G[missing[0]["goal_id"]]["sent"] if missing else None
    R.add("goal_outcomes", "Every goal sent has an outcome per scoped robot", st,
          f"{len(G)} goals; {len(missing)} without outcome, {len(noplan)} no_plan", first=first,
          items=missing + noplan)  # fmt: skip


def check_plans(M: Mission, R: Report, G: dict):
    used: dict[str, dict] = {}
    for g in G.values():
        for robot, pid in g["plan_ids"].items():
            used.setdefault(pid, {"robot": robot, "via": "goal.stage"})
    for m in M.robots:
        for r in M.of(m, "action.start", "action.end"):
            pid = r.get("plan_id") or (r.get("payload") or {}).get("plan_id")
            if pid and M.in_mission(m, r):
                used.setdefault(pid, {"robot": m, "via": r.get("type"), "rec": r})
    solved: dict[str, dict] = {}
    for r in M.of(BASE, "plan.solved"):
        p = r.get("payload") or {}
        for pid in {
            r.get("plan_id"),
            p.get("plan_id"),
            *((p.get("plan_ids") or {}).values()),
        }:
            if pid:
                solved[pid] = r
    if not used:
        return R.add(
            "plans",
            "Every plan used has plan.solved (solve_s, path_len_m)",
            PASS,
            "no plans used",
        )
    missing = [pid for pid in used if pid not in solved]
    no_len, no_solve = [], []
    for pid in used:
        if pid in solved:
            p = solved[pid].get("payload") or {}
            if p.get("solve_s") is None:
                no_solve.append(pid)
            if not p.get("path_len_m"):
                no_len.append(pid)
    st = FAIL if (missing or no_solve) else WARN if no_len else PASS
    first = used[missing[0]].get("rec") if missing else None
    R.add("plans", "Every plan used has plan.solved (solve_s, path_len_m)", st,
          f"{len(used)} plans; {len(missing)} without plan.solved, {len(no_solve)} without solve_s, "
          f"{len(no_len)} without path_len_m", first=first,
          items=[{"plan_id": p, "why": "no plan.solved"} for p in missing]
          + [{"plan_id": p, "why": "no solve_s"} for p in no_solve]
          + [{"plan_id": p, "why": "no path_len_m"} for p in no_len])  # fmt: skip


def check_actions(M: Mission, R: Report):
    dangling, n = [], 0
    for m in M.robots:
        open_: dict = {}
        for r in M.of(m, "action.start", "action.end"):
            p = r.get("payload") or {}
            key = (r.get("plan_id") or p.get("plan_id"), p.get("i"))
            if r["type"] == "action.start":
                if M.in_mission(m, r):
                    open_[key] = r
                    n += 1
            else:
                open_.pop(key, None)
        dangling += [(m, r) for r in open_.values()]
    if not n:
        return R.add(
            "actions",
            "Every action.start has an action.end",
            PASS,
            "no actions in the mission",
        )
    R.add("actions", "Every action.start has an action.end", FAIL if dangling else PASS,
          f"{n} actions, {len(dangling)} dangling", first=dangling[0][1] if dangling else None,
          items=[{"machine": m, "plan_id": r.get("plan_id"), "i": (r.get("payload") or {}).get("i")}
                 for m, r in dangling])  # fmt: skip


def check_leases(M: Mission, R: Report):
    ints = [
        (m, r)
        for m in M.robots
        for r in M.of(m, "lease.intervention")
        if M.in_mission(m, r)
    ]
    if not ints:
        return R.add("lease_timeline", "Every lease intervention is in the base goal timeline", PASS,
                     "no lease interventions")  # fmt: skip
    base_marks = []
    for r in M.base:
        p = r.get("payload") or {}
        if r.get("type") == "goal.stage" and p.get("to") in LEASE_BASE_STAGES:
            base_marks.append((p.get("robot") or r.get("robot"), float(r["t_wall"])))
        elif r.get("type") in ("op.take_lease",) or str(r.get("type", "")).startswith(
            "lease."
        ):
            base_marks.append((p.get("robot") or r.get("robot"), float(r["t_wall"])))
    live = {}
    for m in M.robots:
        for r in iter_jsonl(M.dir / m / LIVE_FILE):
            if r.get("type") == "lease.intervention":
                live.setdefault(m, set()).add((r.get("run_id"), r.get("seq")))
    missing = []
    for m, r in ints:
        t = M.base_t(m, r)
        ok = any(rb == m and abs(tb - t) <= LEASE_MATCH_S for rb, tb in base_marks)
        ok = ok or (r.get("run_id"), r.get("seq")) in live.get(m, set())
        if not ok:
            missing.append((m, r))
    R.add("lease_timeline", "Every lease intervention is in the base goal timeline", FAIL if missing else PASS,
          f"{len(ints)} interventions, {len(missing)} not seen on the base",
          first=missing[0][1] if missing else None)  # fmt: skip


def _refs(obj, out):
    if isinstance(obj, dict):
        if "$file" in obj:
            out.append(obj)
        for v in obj.values():
            _refs(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _refs(v, out)


def _sha(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def resolve_ref(mdir: pathlib.Path, machine: str, ref: str) -> pathlib.Path | None:
    """A ``$file`` of a base event is relative to the base dir (the event root)
    or already inside the mission; robot refs are relative to their run."""
    cands = []
    p = pathlib.Path(ref)
    if p.is_absolute():
        cands.append(p)
    if machine == BASE:
        cands += [mdir.parent.parent / ref, mdir / ref]
        parts = p.parts
        if len(parts) > 2 and parts[0] == "missions" and parts[1] == mdir.name:
            cands.append(mdir.joinpath(*parts[2:]))
    else:
        cands += [mdir / machine / ref] + [
            d / ref for d in (mdir / machine / "runs").glob("*")
        ]
    for c in cands:
        if c.exists():
            return c
    return None


def check_images(M: Mission, R: Report):
    n, missing, bad = 0, [], []
    for m, recs in M.by_machine.items():
        for r in recs:
            if r["_src"] == "live":
                continue
            refs = []
            _refs(r.get("payload"), refs)
            p = r.get("payload") or {}
            if r.get("type") == "goal.images":
                for x in p.get("refs") or []:
                    if x.get("error") and "$file" not in x:
                        missing.append((m, r, x.get("src") or x.get("sym")))
            for ref in refs:
                n += 1
                path = resolve_ref(M.dir, m, str(ref["$file"]))
                if path is None:
                    if m == BASE:
                        missing.append((m, r, ref["$file"]))
                    continue
                if ref.get("sha256") and _sha(path) != ref["sha256"]:
                    bad.append((m, r, ref["$file"]))
    st = FAIL if (missing or bad) else PASS
    first = (missing or bad or [(None, None, None)])[0][1]
    R.add("images", "Images referenced by events exist (and match their sha)", st,
          f"{n} refs; {len(missing)} missing, {len(bad)} sha mismatch", first=first,
          items=[{"machine": m, "ref": f} for m, _, f in missing + bad])  # fmt: skip


def check_clock(M: Mission, R: Report):
    items, worst = [], PASS
    for robot in M.robots:
        samples = [
            (t, o)
            for t, o in M.offsets.samples.get(robot, [])
            if M.t0 - PAD_S <= t <= M.t1 + PAD_S
        ]
        if not samples:
            items.append(
                {"robot": robot, "status": FAIL, "why": "no clock.offset samples"}
            )
            worst = FAIL
            continue
        mx = max(abs(o) for _, o in samples)
        st = FAIL if mx > CLOCK_FAIL_S else WARN if mx > CLOCK_WARN_S else PASS
        items.append(
            {"robot": robot, "status": st, "n": len(samples), "max_abs_s": round(mx, 3)}
        )
        worst = max(worst, st, key=ORDER.get)
    if not M.robots:
        return R.add("clock", "Clock offsets sampled and small", SKIP, "no robots")
    R.add("clock", "Clock offsets sampled and small", worst,
          "; ".join(f"{i['robot']}: " + (f"{i['max_abs_s']} s max over {i['n']}" if "n" in i else i["why"])
                    for i in items), items=items)  # fmt: skip


def executing_intervals(M: Mission, G: dict) -> dict[str, list[tuple[float, float]]]:
    out: dict[str, list] = {}
    for g in G.values():
        cur: dict[str, float] = {}
        for r in sorted(g["stages"], key=lambda x: x["t_wall"]):
            p = r.get("payload") or {}
            robot = p.get("robot")
            if not robot:
                continue
            t = float(r["t_wall"])
            if p.get("to") == "executing" and robot not in cur:
                cur[robot] = t
            elif p.get("to") != "executing" and robot in cur:
                out.setdefault(robot, []).append((cur.pop(robot), t))
        for robot, t in cur.items():
            out.setdefault(robot, []).append((t, M.t1))
    return out


def check_gaps(M: Mission, R: Report, G: dict):
    iv = executing_intervals(M, G)
    if not iv:
        return R.add(
            "gaps", "No event gaps while a goal executes", PASS, "no goal executed"
        )
    items, worst = [], PASS
    for robot, spans in iv.items():
        for machine, sources in (
            (robot, HEARTBEAT_SOURCES["robot"]),
            (BASE, HEARTBEAT_SOURCES["base"]),
        ):
            for src in sources:
                ts = sorted(M.base_t(machine, r) for r in M.of(machine, src=src)
                            if machine == robot or (r.get("robot") == robot or (r.get("payload") or {}).get("robot") == robot))  # fmt: skip
                if not ts:
                    items.append(
                        {
                            "robot": robot,
                            "machine": machine,
                            "source": src,
                            "status": WARN,
                            "why": "no events",
                        }
                    )
                    worst = max(worst, WARN, key=ORDER.get)
                    continue
                for a, b in spans:
                    pts = [a] + [t for t in ts if a < t < b] + [b]
                    gap = max(y - x for x, y in zip(pts, pts[1:]))
                    st = (
                        FAIL if gap > GAP_FAIL_S else WARN if gap > GAP_WARN_S else PASS
                    )
                    if st != PASS:
                        items.append({"robot": robot, "machine": machine, "source": src, "status": st,
                                      "gap_s": round(gap, 1), "span": [a, b]})  # fmt: skip
                    worst = max(worst, st, key=ORDER.get)
    R.add("gaps", "No event gaps while a goal executes", worst,
          f"{sum(len(v) for v in iv.values())} executing spans; " + (f"{len(items)} gaps" if items else "no gaps"),
          items=items)  # fmt: skip


def check_llm(M: Mission, R: Report):
    calls = [r for r in M.of(BASE, "llm.call") if M.in_mission(BASE, r, pad=PAD_S)]
    if not calls:
        return R.add(
            "llm", "LLM calls have model, latency and tokens", PASS, "no LLM calls"
        )
    bad = []
    for r in calls:
        p = r.get("payload") or {}
        if not (p.get("model") or p.get("resp_model")) or p.get("latency_ms") is None:
            bad.append(r)
        elif p.get("ok", True) and (
            p.get("in_tok") is None or p.get("out_tok") is None
        ):
            bad.append(r)
    R.add("llm", "LLM calls have model, latency and tokens", FAIL if bad else PASS,
          f"{len(calls)} calls, {len(bad)} incomplete", first=bad[0] if bad else None)  # fmt: skip


def secret_values(environ=None) -> list[bytes]:
    """Values of secret-looking environment variables (never printed)."""
    env = os.environ if environ is None else environ
    return [
        v.encode() for k, v in env.items() if SECRET_ENV.search(k) and v and len(v) >= 8
    ]


def scan_secrets(mdir, environ=None) -> list[dict]:
    """``[{path, line, pattern}]`` of secret hits (the value itself is never returned)."""
    mdir = pathlib.Path(mdir)
    values = secret_values(environ)
    hits = []
    for p in sorted(mdir.rglob("*")):
        if (
            not p.is_file()
            or p.name in ("report.json",)
            or p.stat().st_size > SCAN_MAX_BYTES
        ):
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192] and p.suffix not in (
            ".jsonl",
            ".json",
            ".txt",
            ".csv",
            ".log",
            ".pddl",
        ):
            continue  # binary (images, bags, sqlite)
        for name, rx in SECRET_PATTERNS:
            m = rx.search(data)
            if m:
                hits.append({"path": str(p.relative_to(mdir)), "line": data.count(b"\n", 0, m.start()) + 1,
                             "pattern": name})  # fmt: skip
        for i, v in enumerate(values):
            k = data.find(v)
            if k >= 0:
                hits.append({"path": str(p.relative_to(mdir)), "line": data.count(b"\n", 0, k) + 1,
                             "pattern": f"env_secret_{i}"})  # fmt: skip
    return hits


def check_secrets(M: Mission, R: Report, environ=None):
    hits = scan_secrets(M.dir, environ)
    R.add("secrets", "No secrets in any file (blocks export)", FAIL if hits else PASS,
          f"{len(hits)} hits" + (f" (first: {hits[0]['path']}:{hits[0]['line']} {hits[0]['pattern']})" if hits else ""),
          items=hits)  # fmt: skip


def run(mdir, write: bool = True, environ=None) -> dict:
    M = Mission(mdir)
    R = Report()
    G = goals(M)
    for fn in (check_machines, check_mission_edges, check_reloc, check_bags):
        fn(M, R)
    check_goals(M, R, G)
    check_plans(M, R, G)
    check_actions(M, R)
    check_leases(M, R)
    check_images(M, R)
    check_clock(M, R)
    check_gaps(M, R, G)
    check_llm(M, R)
    check_secrets(M, R, environ)
    report = {
        "v": 1,
        "mission_id": M.mid,
        "status": R.status,
        "t_checked": time.time(),
        "export_blocked": any(
            c["id"] == "secrets" and c["status"] == FAIL for c in R.checks
        ),
        "counts": {
            s: sum(1 for c in R.checks if c["status"] == s)
            for s in (PASS, WARN, FAIL, SKIP)
        },
        "checks": R.checks,
    }
    if write:
        write_json(M.dir / "report.json", report)
    return report


def format_table(report: dict) -> str:
    rows = [f"mission {report['mission_id']}: {report['status'].upper()}"]
    for c in report["checks"]:
        rows.append(f"  {c['status'].upper():5} {c['id']:15} {c['detail'][:150]}")
        if c["status"] == FAIL and c.get("first"):
            rows.append(f"        first: {json.dumps(c['first'], default=str)[:300]}")
    return "\n".join(rows)
