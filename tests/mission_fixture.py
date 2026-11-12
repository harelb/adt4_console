"""A small synthetic mission on two simulated machines (mission logging tests).

``make(tmp)`` writes the homes of a base station ``willow`` and a Spot ``euclid``
as the real stack leaves them (per-run dirs, events/*.jsonl, a mission bag's
metadata.yaml, executor logs), with one session, one relocalization, one mission
of 120 s and one goto goal that runs to ``done`` (plan P1, a lease takeover in
the middle).  ``skew`` puts the robot's clock ahead of the base's.

``collect(fx)`` runs the real collector over it with a fake ssh / rsync that maps
the two machines to their homes (the slicer really runs as a subprocess).
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess

T = 1794450000.0  # mission start (base clock)
SID = "s20261111-200000_aaaa"
MID = "m20261111-200500_bbbb"
EXP = "fx_exp"
BASE_RID = "willow_20261111-200000_0001"
ROBOT_RID = "euclid_20261111-200001_0002"
PLAN = "plan-0001"
GOAL = "g1794450005-1"
DUR = 120.0


class _Ev:
    def __init__(self, path, machine, robot, run_id, source):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "a")  # noqa: SIM115
        self.env = {
            "machine": machine,
            "robot": robot,
            "run_id": run_id,
            "session_id": SID,
            "source": source,
        }
        self.seq = 0

    def __call__(
        self,
        t,
        type,
        payload=None,
        mission=True,
        goal_id=None,
        plan_id=None,
        robot=None,
    ):  # noqa: A002
        self.seq += 1
        rec = {"v": 1, "t_wall": round(t, 3), "t_mono": round(t - T + 1000, 3), "t_ros": round(t, 3), **self.env,
               "mission_id": MID if mission else None, "goal_id": goal_id, "plan_id": plan_id, "type": type,
               "seq": self.seq, "payload": payload or {}}  # fmt: skip
        if robot:
            rec["robot"] = robot
        self.f.write(json.dumps(rec) + "\n")
        return rec

    def close(self):
        self.f.close()


BAG_META = """rosbag2_bagfile_information:
  version: 9
  storage_identifier: mcap
  duration:
    nanoseconds: {dur}
  starting_time:
    nanoseconds_since_epoch: {start}
  message_count: {total}
  topics_with_message_count:
{topics}
"""


def _bag(run: pathlib.Path, t0: float, t1: float, topics: dict, robot: str):
    d = run / "mission_bag"
    d.mkdir(parents=True, exist_ok=True)
    (d / "mission_bag_0.mcap").write_bytes(b"\x89MCAP0\r\n" + bytes(64))
    tl = "".join(
        f"    - topic_metadata:\n        name: {n}\n        type: std_msgs/msg/String\n"
        f"        serialization_format: cdr\n      message_count: {c}\n"
        for n, c in topics.items()
    )
    (d / "metadata.yaml").write_text(BAG_META.format(dur=int((t1 - t0) * 1e9), start=int(t0 * 1e9),
                                                     total=sum(topics.values()), topics=tl))  # fmt: skip
    lines = ["# mission bag topics (fixture)"]
    for n in topics:
        lines.append(n.replace(f"/{robot}/", "/${ADT4_ROBOT_NAME}/"))
    lines.append("/${ADT4_ROBOT_NAME}/spot_executor_node/pause  # optional")
    (run / "mission_bag_topics.txt").write_text("\n".join(lines) + "\n")


def make(
    tmp, skew: float = 0.0, robot_events: bool = True, secret: str | None = None
) -> dict:
    tmp = pathlib.Path(tmp)
    willow, euclid = tmp / "willow", tmp / "euclid"
    bexp, rexp = willow / "adt4_output" / EXP, euclid / "adt4_output" / EXP
    brun, rrun = bexp / "runs" / BASE_RID, rexp / "runs" / ROBOT_RID
    for run, rid, machine, robot, plat in ((brun, BASE_RID, "willow", "willow", "base"),
                                           (rrun, ROBOT_RID, "euclid", "euclid", "spot")):  # fmt: skip
        run.mkdir(parents=True, exist_ok=True)
        (run / "run_info.json").write_text(json.dumps({
            "run_id": rid, "machine": machine, "robot": robot, "platform": plat, "session_id": SID,
            "experiment": EXP, "t_start": T - 400, "git": {"superproject": "abc123", "submodules": {"fleetlink": "f1"}},
            "git_commit": "abc123"}))  # fmt: skip
    (bexp / "latest").symlink_to(f"runs/{BASE_RID}")
    (rexp / "latest").symlink_to(f"runs/{ROBOT_RID}")
    # ---- session / mission records (what the logbook writes)
    sdir = bexp / "sessions" / SID
    sdir.mkdir(parents=True)
    reloc = {"session_id": SID, "t": T - 100, "robots": {"euclid": {"state": "relocalized", "t": T - 100,
                                                                     "operator": "harel", "method": "drive"}}}  # fmt: skip
    (sdir / "session.json").write_text(json.dumps({
        "session_id": SID, "state": "stopped", "t_start": T - 400, "t_end": T + 400, "experiment": EXP,
        "mode": "relocalize", "robots": ["euclid"], "base_station": "willow",
        "launch": {"experiment": EXP, "mode": "relocalize", "robots": ["euclid"], "prior_path": "~/adt4_output/prior"},
        "machines": ["euclid", "willow"]}))  # fmt: skip
    (sdir / "reloc.json").write_text(json.dumps(reloc))
    mdir = bexp / "missions" / MID
    mdir.mkdir(parents=True)
    (mdir / "mission.json").write_text(json.dumps({
        "mission_id": MID, "name": "fixture run", "state": "ended", "session_id": SID, "experiment": EXP,
        "t_start": T, "t_end": T + DUR, "start_reason": "operator", "end_reason": "operator", "robots": ["euclid"],
        "goals": [GOAL], "reloc": {"euclid": "relocalized"}, "run_ids": {"willow": "willow_console"}}))  # fmt: skip
    (mdir / "reloc.json").write_text(json.dumps(reloc))
    img = mdir / "images" / "goals" / GOAL / "O12_0.jpg"
    img.parent.mkdir(parents=True)
    img.write_bytes(b"\xff\xd8\xff\xe0fixture-jpeg\xff\xd9")
    # ---- base events: console + goal_tracker (console root), planner (base run)
    con = _Ev(
        bexp / "events" / "console.jsonl",
        "willow",
        "willow",
        "willow_console",
        "console",
    )
    gt = _Ev(
        bexp / "events" / "goal_tracker.jsonl",
        "willow",
        "euclid",
        "willow_console",
        "goal_tracker",
    )
    om = _Ev(
        brun / "events" / "omniplanner.jsonl",
        "willow",
        "willow",
        BASE_RID,
        "omniplanner",
    )
    con(T - 400, "session.start", {"session_id": SID}, mission=False)
    con(T - 300, "reloc.started", {"robot": "euclid"}, mission=False, robot="euclid")
    con(T - 100, "reloc.confirmed", {"robot": "euclid", "hydra_accepted": 4, "hydra_total": 6}, mission=False,
        robot="euclid")  # fmt: skip
    con(
        T,
        "mission.start",
        {"mission_id": MID, "reason": "operator", "robots": ["euclid"]},
    )
    for k in range(-2, 7):
        con(T + 30 * k, "clock.offset", {"robot": "euclid", "offset_ms": skew * 1000.0, "rtt_ms": 40.0},
            mission=0 <= k <= 4, robot="euclid")  # fmt: skip
    for k in range(0, 25):
        con(T + 5 * k, "link.stats", {"robot": "euclid", "latency_ms": 60 + k, "rtt_ms": 120, "loss": {"C1": 0.01}},
            robot="euclid")  # fmt: skip
    for k in range(-5, int(DUR) + 5):
        x = 0.0 if k < 10 else min(10.0, (k - 10) * 0.25)
        con(T + k, "robot.pose", {"robot": "euclid", "x": x, "y": 0.0, "z": 0.0, "yaw": 0.0, "frame": "map"},
            mission=0 <= k <= DUR, robot="euclid")  # fmt: skip
    con(T + 2, "chat.user", {"text": "go to the mug"})
    con(T + 3, "llm.call", {"provider": "openai", "model": "gpt-x", "resp_model": "gpt-x-2026", "latency_ms": 900,
                            "ttft_ms": 300, "in_tok": 1200, "out_tok": 80, "cost_usd": 0.004, "ok": True})  # fmt: skip
    con(
        T + 3.5,
        "chat.tool_call",
        {"name": "propose_goal", "args": {"robot": "euclid", "target": "O12"}},
    )
    con(
        T + 4,
        "chat.assistant",
        {"text": "Sending euclid to O(12)." + (f" key {secret}" if secret else "")},
    )
    con(
        T + 4.5,
        "goal.proposed",
        {"card": {"robot": "euclid", "kind": "goto", "target": "O(12)"}},
        goal_id=GOAL,
    )
    con(T + 5, "goal.sent", {"robot": "euclid", "robots": ["euclid"], "kind": "goto", "payload": {"target": "O(12)"},
                             "track_id": GOAL}, goal_id=GOAL)  # fmt: skip
    con(T + 6, "goal.images", {"goal_id": GOAL, "refs": [{"sym": "O(12)", "i": 0, "src": "/store/1.jpg",
                                                         "$file": f"missions/{MID}/images/goals/{GOAL}/O12_0.jpg"}]},
        goal_id=GOAL)  # fmt: skip
    con(T + 40, "mission.ack", {"robot": "euclid", "mission_id": MID}, robot="euclid")
    con(
        T + DUR,
        "mission.end",
        {"mission_id": MID, "reason": "operator", "duration_s": DUR},
    )
    stages = [(5, None, "planning", None), (5.1, "euclid", "waiting", None), (8, "euclid", "planned", PLAN),
              (9, "euclid", "delivered", PLAN), (10, "euclid", "executing", PLAN), (31, "euclid", "lease_wait", PLAN),
              (35, "euclid", "executing", PLAN), (60, "euclid", "done", PLAN), (60, None, "done", None)]  # fmt: skip
    prev: dict = {}
    for dt, r, to, pid in stages:
        gt(T + dt, "goal.stage", {"goal_id": GOAL, "robot": r, "from": prev.get(r), "to": to, "plan_id": pid,
                                  "kind": "goto"}, goal_id=GOAL, plan_id=pid, robot=r or "euclid")  # fmt: skip
        prev[r] = to
    gt(T + 60, "goal.outcome", {"goal_id": GOAL, "robot": "euclid", "result": "done", "t_sent": T + 5,
                                "t_done": T + 60, "duration_s": 55.0}, goal_id=GOAL, plan_id=PLAN)  # fmt: skip
    gt(
        T + 60,
        "goal.outcome",
        {"goal_id": GOAL, "robot": None, "result": "done", "duration_s": 55.0},
        goal_id=GOAL,
    )
    om(
        T + 5,
        "plan.request",
        {"request": 1, "plugin": "goto_points", "robots": ["euclid"]},
    )
    om(T + 8, "plan.solved", {"request": 1, "plugin": "goto_points", "plan_id": PLAN, "engine": "goto_points",
                              "solve_s": 2.9, "plan_ids": {"euclid": PLAN}, "n_actions": {"euclid": 1},
                              "actions": {"euclid": [{"type": "FOLLOW", "n_points": 3, "len_m": 10.0,
                                                      "path": [[0, 0], [5, 0], [10, 0]]}]},
                              "path_len_m": {"euclid": 10.0}}, plan_id=PLAN)  # fmt: skip
    om(T + 8.1, "plan.published", {"plan_id": PLAN, "robot": "euclid"}, plan_id=PLAN)
    for e in (con, gt, om):
        e.close()
    live = bexp / "events" / "live"
    live.mkdir(parents=True)
    # ---- robot events (robot clock = base + skew)
    if robot_events:
        ex = _Ev(
            rrun / "events" / "spot_executor.jsonl",
            "euclid",
            "euclid",
            ROBOT_RID,
            "spot_executor",
        )
        ag = _Ev(
            rrun / "events" / "fleetlink_agent.jsonl",
            "euclid",
            "euclid",
            ROBOT_RID,
            "fleetlink_agent",
        )
        s = skew
        ex(T + 9.5 + s, "plan.received", {"plan_id": PLAN, "n": 1}, plan_id=PLAN)
        a0 = ex(
            T + 10 + s,
            "action.start",
            {"plan_id": PLAN, "i": 0, "n": 1, "type": "FOLLOW"},
            plan_id=PLAN,
        )
        for k in range(0, 24):
            ex(
                T + 11 + 2 * k + s,
                "follow.progress",
                {"plan_id": PLAN, "frac": k / 24},
                plan_id=PLAN,
            )
        li = ex(T + 30 + s, "lease.intervention", {"kind": "manual", "owner": "tablet", "plan_id": PLAN},
                plan_id=PLAN)  # fmt: skip
        ex(
            T + 58 + s,
            "action.end",
            {"plan_id": PLAN, "i": 0, "ok": True, "duration_s": 48},
            plan_id=PLAN,
        )
        ag(T + 0.5 + s, "mission.ack", {"mission_id": MID})
        for k in range(-5, int(DUR) + 5):
            ag(T + k + s, "robot.pose", {"x": 0.0 if k < 10 else min(10.0, (k - 10) * 0.25), "y": 0.0, "yaw": 0.0,
                                         "frame": "euclid/map"}, mission=0 <= k <= DUR)  # fmt: skip
            if k % 10 == 0:
                ag(T + k + s, "loc.snapshot", {"why": "periodic", "prior_lcs": 3, "reloc_ok": True,
                                               "map_T_odom": [0, 0, 0, 1, 0, 0, 0]})  # fmt: skip
        ex.close()
        ag.close()
        with open(live / "euclid.jsonl", "w") as f:
            for r in (a0, li):
                f.write(json.dumps(r) + "\n")
    sp = rrun / "spot_executor"
    sp.mkdir(parents=True)
    (sp / "str_log.txt").write_text("# start fixture\nfollowing path\n")
    (sp / "lease_log.txt").write_text(
        "time,event\n%f,manual_intervention\n" % (T + 30 + skew)
    )
    hb = rrun / "hydra_multi" / "backend"
    hb.mkdir(parents=True)
    (hb / "loop_closures.csv").write_text("t,from,to\n")
    (hb / "robot_0_trajectory.csv").write_text("t,x,y\n")
    _bag(brun, T - 300, T + 300, {"/willow/omniplanner_node/plan_status": 12, "/euclid/omniplanner_node/compiled_plan_out": 1,
                                   "/willow/goal_manager/robot_plan_states": 30}, "willow")  # fmt: skip
    _bag(rrun, T - 300 + skew, T + 300 + skew, {"/euclid/odom": 6000, "/euclid/spot_executor_node/action_result": 1,
                                                "/euclid/spot_executor_node/runtime_guards": 300}, "euclid")  # fmt: skip
    return {
        "root": tmp,
        "willow": willow,
        "euclid": euclid,
        "base_exp": bexp,
        "robot_exp": rexp,
        "mdir": mdir,
    }


def fake_ssh(fx):
    homes = {"willow": fx["willow"], "euclid": fx["euclid"]}

    def ssh(user, ip, cmd, timeout=5):
        home = homes[ip]
        cmd = cmd.replace("~/adt4_output", str(home / "adt4_output"))
        r = subprocess.run(
            ["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout + 30
        )
        return r.returncode, r.stdout, r.stderr

    return ssh


def fake_rsync(fx, calls=None):
    homes = {"willow": fx["willow"], "euclid": fx["euclid"]}

    def rsync(
        su,
        sip,
        sp,
        du,
        dip,
        dp,
        exclude=None,
        progress_callback=None,
        bwlimit_kbps=None,
    ):
        if calls is not None:
            calls.append(
                {"src": sp, "dst": dp, "bwlimit_kbps": bwlimit_kbps, "exclude": exclude}
            )
        src = sp.replace("~/adt4_output", str(homes[sip] / "adt4_output"))
        os.makedirs(dp, exist_ok=True)
        cmd = (
            ["rsync", "-a"]
            + [x for e in exclude or [] for x in ("--exclude", e)]
            + [src + "/", dp + "/"]
        )
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.returncode == 0, "direct", r.stderr

    return rsync


def endpoint(name):
    n = name or "willow"
    return {"name": n, "user": "swarm", "ip": n}


def collector(fx, **kw):
    from adt4_console.mission_log.collector import Collector

    kw.setdefault("ssh", fake_ssh(fx))
    kw.setdefault("rsync", fake_rsync(fx))
    return Collector(
        endpoint=endpoint, live_root=str(fx["base_exp"]), settle_s=0.0, **kw
    )


def collect(fx, **kw):
    return collector(fx, **kw).collect(fx["mdir"], wait=True)
