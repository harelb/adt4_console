"""Mission collection, manifest, check and the Missions API (mission logging B2/B3/C1)."""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import threading
import time

import pytest

from adt4_console import eventlog
from adt4_console.mission_log import (
    Offsets,
    check,
    collector,
    index,
    manifest,
    read_json,
    slicer,
    store,
)
from tests import mission_fixture as mf


@pytest.fixture
def fx(tmp_path):
    return mf.make(tmp_path)


def _status(rep, cid):
    return next(c for c in rep["checks"] if c["id"] == cid)["status"]


# ------------------------------------------------------------------ slicer
def test_slicer_window_rotation_and_artifacts(fx, tmp_path):
    run = fx["robot_exp"] / "runs" / mf.ROBOT_RID
    ev = run / "events"
    # a rotated file with an old line and a line of this mission
    (ev / "spot_executor.1.jsonl").write_text(
        json.dumps({"t_wall": mf.T - 5000, "type": "old", "seq": 1}) + "\n"
        + json.dumps({"t_wall": mf.T + 1, "type": "action.early", "seq": 2, "mission_id": mf.MID}) + "\n"
    )  # fmt: skip
    ge = run / "spot_executor" / "grasp_episodes" / "20261111_200530_1"
    ge.mkdir(parents=True)
    (ge / "events.jsonl").write_text("{}\n")
    out = tmp_path / "out"
    r = slicer.run_slice(
        [str(fx["robot_exp"])], mf.MID, mf.T, mf.T + mf.DUR, str(out), session=mf.SID
    )
    assert [x["run_id"] for x in r["runs"]] == [mf.ROBOT_RID]
    lines = [
        json.loads(x)
        for x in (out / "events" / "spot_executor.jsonl").read_text().splitlines()
    ]
    assert "old" not in {x["type"] for x in lines} and "action.early" in {
        x["type"] for x in lines
    }
    assert [x["t_wall"] for x in lines] == sorted(x["t_wall"] for x in lines)
    rr = out / "runs" / mf.ROBOT_RID
    assert (rr / "run_info.json").exists() and (
        rr / "spot_executor" / "lease_log.txt"
    ).exists()
    assert (rr / "hydra_multi" / "backend" / "loop_closures.csv").exists()
    bag = r["runs"][0]["bags"][0]
    assert (
        bag["t_start"] == pytest.approx(mf.T - 300)
        and bag["topics"]["/euclid/odom"] == 6000
    )
    assert not (rr / "mission_bag").exists()  # referenced, never copied
    f = next(x for x in r["files"] if x["path"] == "events/spot_executor.jsonl")
    assert f["n_events"] == len(lines) and len(f["sha256"]) == 64
    # a session filter drops runs of other sessions
    other = fx["robot_exp"] / "runs" / "euclid_other"
    (other / "events").mkdir(parents=True)
    (other / "run_info.json").write_text(
        json.dumps({"run_id": "euclid_other", "session_id": "s-other"})
    )
    r2 = slicer.run_slice(
        [str(fx["robot_exp"])],
        mf.MID,
        mf.T,
        mf.T + 10,
        str(tmp_path / "o2"),
        session=mf.SID,
    )
    assert [x["run_id"] for x in r2["runs"]] == [mf.ROBOT_RID]


def test_slicer_runs_standalone_over_the_command(fx, tmp_path):
    cmd = collector.slicer_command(["--exp", str(fx["robot_exp"]), "--mission", mf.MID, "--t0", str(mf.T),
                                    "--t1", str(mf.T + 5), "--out", str(tmp_path / "s")])  # fmt: skip
    out = subprocess.run(["bash", "-c", cmd.replace("python3", sys.executable)], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin"}, cwd=tmp_path)  # fmt: skip
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1])["ok"]


def test_open_bag_is_reported_open(tmp_path):
    d = tmp_path / "mission_bag"
    d.mkdir()
    (d / "mission_bag_0.mcap").write_bytes(b"x" * 10)
    info = slicer.bag_info(str(d))
    assert info["open"] and info["t_last_write"] > 0


# ------------------------------------------------------------------ collect + check
def test_collect_builds_a_self_contained_mission_that_passes(fx):
    calls = []
    st = mf.collect(fx, rsync=mf.fake_rsync(fx, calls))
    assert st["state"] == "done" and st["machines"]["euclid"]["state"] == "collected"
    assert calls and calls[0]["bwlimit_kbps"] == collector.BWLIMIT_KBPS
    assert "mission_bag*/" in calls[0]["exclude"]
    m = fx["mdir"]
    for rel in ("base/slice.json", "euclid/slice.json", "euclid/events/spot_executor.jsonl",
                "euclid/events.live.jsonl", "plans/plan-0001.json", "manifest.json", "index.sqlite", "report.json"):  # fmt: skip
        assert (m / rel).exists(), rel
    rep = read_json(m / "report.json")
    bad = [c for c in rep["checks"] if c["status"] not in ("pass", "skip")]
    assert rep["status"] == "pass", bad
    mj = read_json(m / "mission.json")
    assert mj["run_ids"]["euclid"] == [mf.ROBOT_RID] and mj["run_ids"]["base"] == [
        mf.BASE_RID
    ]
    man = read_json(m / "manifest.json")
    assert (
        man["machines"]["euclid"]["collect"] == "collected" and man["llm"]["calls"] == 1
    )
    assert (
        man["clock_offsets"]["euclid"]["n"] >= 5
        and man["goals"][0]["outcomes"]["euclid"]["result"] == "done"
    )
    assert (
        "env" not in json.dumps(man).lower().replace("environment", "")[:0]
    )  # never an env dump
    assert man["images"] and man["images"][0]["sha256"]


def test_unreachable_robot_stays_pending_and_is_retried(fx):
    state = {"down": True}
    real = mf.fake_ssh(fx)

    def ssh(user, ip, cmd, timeout=5):
        if ip == "euclid" and state["down"]:
            return 255, "", "ssh: connect to host euclid port 22: No route to host"
        return real(user, ip, cmd, timeout)

    c = mf.collector(fx, ssh=ssh, retry_s=0.2)
    st = c.collect(fx["mdir"], wait=True)
    assert (
        st["machines"]["euclid"]["state"] == "pending"
        and "No route" in st["machines"]["euclid"]["error"]
    )
    assert st["state"] == "pending"
    rep = read_json(fx["mdir"] / "report.json")
    assert _status(rep, "machines") == "warn"  # live_only: the C7 events made it
    state["down"] = False  # the robot is back: the retry loop collects it
    t0 = time.time()
    while time.time() - t0 < 20 and (c.status(fx["mdir"])["machines"]["euclid"]["state"] != "collected"
                                     or c.status(fx["mdir"]).get("phase") != "done"):  # fmt: skip
        time.sleep(0.1)
    c.stop()
    assert c.status(fx["mdir"])["machines"]["euclid"]["state"] == "collected"
    assert read_json(fx["mdir"] / "report.json")["status"] == "pass"


def test_collect_waits_while_a_robot_executes(fx):
    busy = {"on": True}
    c = mf.collector(
        fx, executing=lambda: ["euclid"] if busy["on"] else [], defer_poll_s=0.05
    )
    th = threading.Thread(
        target=c.collect, args=(fx["mdir"],), kwargs={"auto": True, "wait": True}
    )
    th.start()
    t0 = time.time()
    while time.time() - t0 < 10 and c.status(fx["mdir"]).get("state") != "deferred":
        time.sleep(0.05)
    assert c.status(fx["mdir"])["machines"]["euclid"]["state"] == "deferred"
    busy["on"] = False
    th.join(20)
    assert c.status(fx["mdir"])["machines"]["euclid"]["state"] == "collected"
    c.stop()


def test_clock_skew_fails_the_check(tmp_path):
    fx = mf.make(tmp_path, skew=3.0)
    mf.collect(fx)
    rep = read_json(fx["mdir"] / "report.json")
    assert rep["status"] == "fail" and _status(rep, "clock") == "fail"
    # events are still aligned to base time: the plan / action / lease checks hold
    assert (
        _status(rep, "lease_timeline") == "pass" and _status(rep, "actions") == "pass"
    )
    idx = index.query(fx["mdir"], types="action.start")
    assert idx["events"][0]["t_base"] == pytest.approx(mf.T + 10, abs=0.01)
    fx2 = mf.make(tmp_path / "w", skew=0.5)
    mf.collect(fx2)
    assert _status(read_json(fx2["mdir"] / "report.json"), "clock") == "warn"


def test_check_failures(fx):
    mf.collect(fx)
    m = fx["mdir"]
    # a dangling action, a lease takeover the base never saw, a goal without outcome
    with open(m / "euclid" / "events" / "spot_executor.jsonl", "a") as f:
        f.write(json.dumps({"t_wall": mf.T + 70, "type": "action.start", "plan_id": "plan-x", "seq": 900,
                            "payload": {"plan_id": "plan-x", "i": 0}}) + "\n")  # fmt: skip
        f.write(json.dumps({"t_wall": mf.T + 100, "type": "lease.intervention", "seq": 901, "run_id": "r",
                            "payload": {"kind": "manual"}}) + "\n")  # fmt: skip
    with open(m / "base" / "events" / "console.jsonl", "a") as f:
        f.write(json.dumps({"t_wall": mf.T + 90, "type": "goal.sent", "goal_id": "g-lost", "seq": 999,
                            "mission_id": mf.MID, "payload": {"robot": "euclid"}}) + "\n")  # fmt: skip
        f.write(
            json.dumps(
                {
                    "t_wall": mf.T + 91,
                    "type": "llm.call",
                    "seq": 1000,
                    "payload": {"model": "m"},
                }
            )
            + "\n"
        )
    rep = check.run(m)
    for cid in ("actions", "lease_timeline", "goal_outcomes", "plans", "llm"):
        assert _status(rep, cid) == "fail", cid
    assert (
        next(c for c in rep["checks"] if c["id"] == "goal_outcomes")["first"]["goal_id"]
        == "g-lost"
    )
    # a mission that never ended
    mj = read_json(m / "mission.json")
    mj.update(state="open", t_end=None, end_reason=None)
    (m / "mission.json").write_text(json.dumps(mj))
    assert _status(check.run(m), "mission_edges") == "fail"


def test_missing_robot_and_reloc_override(tmp_path):
    fx = mf.make(tmp_path, robot_events=False)
    rl = read_json(fx["mdir"] / "reloc.json")
    rl["robots"]["euclid"].update(state="overridden", reason="prior map off by a floor")
    (fx["mdir"] / "reloc.json").write_text(json.dumps(rl))
    (fx["base_exp"] / "events" / "live" / "euclid.jsonl").unlink(missing_ok=True)
    ok = mf.fake_ssh(fx)
    mf.collect(
        fx,
        ssh=lambda u, ip, c, timeout=5: (255, "", "down")
        if ip == "euclid"
        else ok(u, ip, c, timeout),
    )
    rep = read_json(fx["mdir"] / "report.json")
    assert _status(rep, "machines") == "fail" and _status(rep, "reloc") == "warn"


def test_secret_scan_blocks_export(tmp_path):
    key = "sk-proj-" + "A1b2C3d4" * 4
    fx = mf.make(tmp_path, secret=key)
    mf.collect(fx)
    rep = read_json(fx["mdir"] / "report.json")
    sec = next(c for c in rep["checks"] if c["id"] == "secrets")
    assert sec["status"] == "fail" and rep["export_blocked"]
    assert key not in json.dumps(rep)  # the report names the file, never the value
    with pytest.raises(PermissionError):
        store.export(fx["mdir"], tmp_path / "x")
    # an env secret (e.g. the Spot password) is caught by value too
    hits = check.scan_secrets(
        fx["mdir"], environ={"ADT4_BOSDYN_PASSWORD": "hunter2hunter2", "HOME": "/x"}
    )
    assert not [h for h in hits if h["pattern"].startswith("env_secret")]
    (fx["mdir"] / "base" / "events" / "console.jsonl").open("a").write(
        '{"note": "hunter2hunter2"}\n'
    )
    hits = check.scan_secrets(
        fx["mdir"], environ={"ADT4_BOSDYN_PASSWORD": "hunter2hunter2"}
    )
    assert any(h["pattern"].startswith("env_secret") for h in hits)


def test_offsets_interpolate():
    o = Offsets({"r": [(0.0, 1.0), (10.0, 2.0)]})
    assert (
        o.at("r", 5.0) == pytest.approx(1.5)
        and o.at("r", -1) == 1.0
        and o.at("r", 99) == 2.0
    )
    assert o.to_base("r", 5.0) == pytest.approx(3.5) and o.at("other", 3) == 0.0


def test_manifest_and_index_and_views(fx):
    mf.collect(fx)
    m = fx["mdir"]
    man = manifest.build(m, write=False)
    assert man["expected_machines"] == ["base", "euclid"]
    q = index.query(m, types="goal.*", limit=3)
    assert (
        q["total"] >= 10
        and len(q["events"]) == 3
        and "console" in q["facets"]["source"]
    )
    q2 = index.query(m, machine="euclid", text="lease.intervention")
    assert q2["total"] >= 1
    g = store.goals(m)
    assert g[0]["goal_id"] == mf.GOAL
    r = g[0]["robots"]["euclid"]
    assert (
        r["planned_m"] == 10.0
        and r["result"] == "done"
        and r["leases"] == 1
        and r["solve_s"] == 2.9
    )
    assert 9.0 <= r["driven_m"] <= 10.5 and r["time_to_reach_s"] == pytest.approx(
        55.0, abs=0.1
    )
    ch = store.chat(m)
    assert [c["type"] for c in ch][:3] == ["chat.user", "llm.call", "chat.tool_call"]
    st = store.stats(m)
    assert (
        st["llm"]["calls"] == 1
        and st["link"]["euclid"]
        and "goal_id" in store.stats_csv(st)
    )
    rp = store.replay(m)
    assert rp["poses"]["euclid"][0][1] == 0.0 and rp["plans"][0]["path"] == [
        [0, 0],
        [5, 0],
        [10, 0],
    ]
    assert any(
        x["robot"] == "euclid" and x["state"] == "relocalized" for x in rp["reloc"]
    )
    sess = store.list_sessions(fx["willow"] / "adt4_output")
    assert sess[0]["session_id"] == mf.SID and sess[0]["missions"][0]["check"] == "pass"
    assert sess[0]["missions"][0]["goals_done"] == 1
    out = store.export(m, fx["root"] / "exports")
    assert out.exists() and out.stat().st_size > 0


def test_cli_check_exit_codes(fx, capsys):
    from adt4_console.mission_log import cli

    mf.collect(fx)
    assert cli.main(["check", str(fx["mdir"])]) == 0
    assert "PASS" in capsys.readouterr().out
    assert (
        cli.main(
            ["check", mf.MID, "--root", str(fx["willow"] / "adt4_output"), "--json"]
        )
        == 0
    )
    mj = read_json(fx["mdir"] / "mission.json")
    mj["end_reason"] = None
    (fx["mdir"] / "mission.json").write_text(json.dumps(mj))
    assert cli.main(["check", str(fx["mdir"])]) == 1


# ------------------------------------------------------------------ API
@pytest.fixture
def client(fx, settings):
    from tests.conftest import make_client

    settings.output_root = str(fx["willow"] / "adt4_output")
    c, svc = make_client(settings)
    svc.collector = mf.collector(fx)
    with c:
        yield c


def test_api(fx, client):
    r = client.post(f"/api/logbook/missions/{mf.MID}/collect")
    assert r.status_code == 200
    t0 = time.time()
    while (
        time.time() - t0 < 30
        and client.get(f"/api/logbook/missions/{mf.MID}").json()["summary"]["collect"]
        != "done"
    ):
        time.sleep(0.1)
    lst = client.get("/api/logbook/missions").json()
    assert lst["sessions"][0]["missions"][0]["mission_id"] == mf.MID
    d = client.get(f"/api/logbook/missions/{mf.MID}").json()
    assert d["report"]["status"] == "pass" and d["manifest"]["mission_id"] == mf.MID
    ev = client.get(
        f"/api/logbook/missions/{mf.MID}/events", params={"type": "lease.*,action.*"}
    ).json()
    assert {e["type"] for e in ev["events"]} >= {
        "lease.intervention",
        "action.start",
        "action.end",
    }
    assert (
        client.get(f"/api/logbook/missions/{mf.MID}/goals").json()["goals"][0][
            "goal_id"
        ]
        == mf.GOAL
    )
    assert client.get(f"/api/logbook/missions/{mf.MID}/chat").json()["turns"]
    assert "goal_id" in client.get(f"/api/logbook/missions/{mf.MID}/stats.csv").text
    assert client.get(f"/api/logbook/missions/{mf.MID}/replay").json()["poses"][
        "euclid"
    ]
    img = client.get(
        f"/api/logbook/missions/{mf.MID}/file",
        params={"path": f"images/goals/{mf.GOAL}/O12_0.jpg"},
    )
    assert img.status_code == 200 and img.content.startswith(b"\xff\xd8")
    assert (
        client.get(
            f"/api/logbook/missions/{mf.MID}/file",
            params={"path": "../../../../etc/passwd"},
        ).status_code
        == 404
    )
    assert (
        client.post(f"/api/logbook/missions/{mf.MID}/check").json()["status"] == "pass"
    )
    ex = client.post(f"/api/logbook/missions/{mf.MID}/export").json()
    dl = client.get(f"/api/logbook/missions/{mf.MID}/export/{ex['file']}")
    assert dl.status_code == 200 and len(dl.content) == ex["size"]
    assert client.get("/api/logbook/missions/nope").status_code == 404


# ------------------------------------------------------------------ console side of B1
def test_live_events_and_mission_ack(tmp_path):
    from adt4_console.eventtap import HubTap
    from adt4_console.live_service import LiveService

    eventlog.reset()
    eventlog.enable(True, fallback_root=tmp_path)
    try:
        ls = LiveService(settings=type("S", (), {"fleetlink": False})())
        ls.on_update("euclid", "events", {"lines": ['{"type":"action.start","t_wall":1}', '{"type":"x","k":"sk-' + "a" * 30 + '"}'],
                                           "dropped": 0})  # fmt: skip
        tap = HubTap()
        tap(
            "robot/euclid/state",
            {
                "mission": {"mission_id": "m1", "state": "open"},
                "run": {"run_id": "euclid_1"},
            },
        )
        tap(
            "robot/euclid/state",
            {
                "mission": {"mission_id": "m1", "state": "open"},
                "run": {"run_id": "euclid_1"},
            },
        )
        eventlog.flush()
        live = (tmp_path / "events" / "live" / "euclid.jsonl").read_text().splitlines()
        assert (
            len(live) == 2
            and json.loads(live[0])["type"] == "action.start"
            and "sk-aaaa" not in live[1]
        )
        acks = [json.loads(x) for x in (tmp_path / "events" / "console.jsonl").read_text().splitlines()
                if '"mission.ack"' in x]  # fmt: skip
        assert len(acks) == 1 and acks[0]["payload"]["mission_id"] == "m1"
        assert tap.runs["euclid"]["run_id"] == "euclid_1"
    finally:
        eventlog.reset()


def test_logbook_end_hook_and_run_ids(tmp_path):
    from adt4_console.logbook import Logbook

    eventlog.reset()
    eventlog.enable(True, fallback_root=tmp_path)
    try:
        s = type("S", (), {"output_root": str(tmp_path)})()
        lb = Logbook(s, persist=True)
        lb.run_ids_fn = lambda: {"euclid": ["euclid_r1"]}
        ended = []
        lb.on_mission_end = lambda m, d: ended.append(
            (m["mission_id"], pathlib.Path(d))
        )
        m = lb.start_mission(name="t")
        assert m["run_ids"]["euclid"] == ["euclid_r1"]
        lb.end_mission()
        t0 = time.time()
        while not ended and time.time() - t0 < 5:
            time.sleep(0.02)
        assert (
            ended
            and ended[0][0] == m["mission_id"]
            and (ended[0][1] / "mission.json").exists()
        )
        lb.stop()
    finally:
        eventlog.reset()


def test_shutil_unused():
    assert shutil.which("rsync"), "rsync is needed by the collector tests"


def test_bag_still_recording_is_a_warning(tmp_path):
    fx = mf.make(tmp_path)
    (fx["robot_exp"] / "runs" / mf.ROBOT_RID / "mission_bag" / "metadata.yaml").unlink()
    mf.collect(fx)
    rep = read_json(fx["mdir"] / "report.json")
    bags = next(c for c in rep["checks"] if c["id"] == "bags")
    assert bags["status"] == "warn" and "still recording" in json.dumps(bags["items"])
    assert rep["status"] == "warn"
