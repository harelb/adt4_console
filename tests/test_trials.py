"""Trials: fleet-wide start / status / flags / stop, the event log, and the
operator request log.  The machines' ``adt4-rec`` is a fake behind ssh_cmd."""

import json
import os
import pathlib
import shlex

import pytest

from adt4_console import trials as tr

from .conftest import make_client

GB = 10**9
ROBOTS = ["euclid", "gauss", "hamilton"]


class FakeRec:
    """ssh_cmd that answers like adt4-rec on each machine (keyed by ip)."""

    def __init__(self):
        self.calls = []  # (ip, [args after --output-root ROOT])
        self.rec = {}  # ip -> {"trial", "bytes"}
        self.unreachable = set()
        self.missing = set()
        self.fail_start = {}  # ip -> error
        self.stubborn = set()
        self.free = 200 * GB
        self.rate = 50_000_000  # bytes per status call
        self.t = 1_794_800_000.0
        self.silent = ["/x/odom"]
        self.missing_topics = []

    def ssh_cmd(self, user, ip, cmd, timeout=5):
        assert isinstance(timeout, int)  # it becomes ssh -o ConnectTimeout=N
        argv = shlex.split(cmd)
        assert (
            argv[0] == "python3"
            and argv[1].endswith("adt4-rec")
            and argv[2] == "--output-root"
        )
        args = argv[4:]
        self.calls.append((ip, args))
        if ip in self.unreachable:
            return 255, "", "ssh: connect to host: Connection timed out"
        if ip in self.missing:
            return (
                2,
                "",
                "python3: can't open file '/home/swarm/dcist_ws/.../adt4-rec': [Errno 2] No such file or directory",
            )
        self.t += 0.5
        verb = args[0]
        opt = {
            args[i]: args[i + 1]
            for i in range(1, len(args) - 1)
            if args[i].startswith("--")
        }
        rec = self.rec.get(ip)
        if verb == "status":
            out = {"ok": True, "t": self.t, "recording": rec is not None, "free_bytes": self.free,
                   "total_bytes": 900 * GB, "other_recorders": [], "stack_running": True}  # fmt: skip
            if rec:
                rec["bytes"] += self.rate
                out.update(
                    trial=rec["trial"],
                    bytes=rec["bytes"],
                    last_write_age_s=0.2,
                    pid=4242,
                )
            return 0, "noise from .zshrc\n" + json.dumps(out), ""
        if verb == "start":
            if ip in self.fail_start:
                return 1, json.dumps({"ok": False, "error": self.fail_start[ip]}), ""
            if rec:
                return (
                    3,
                    json.dumps(
                        {
                            "ok": False,
                            "error": "already recording",
                            "recording": True,
                            "trial": rec["trial"],
                        }
                    ),
                    "",
                )
            self.rec[ip] = {"trial": opt["--trial"], "bytes": 1000}
            return 0, json.dumps({"ok": True, "recording": True, "trial": opt["--trial"], "machine": opt["--machine"],
                                  "dir": f"/home/swarm/adt4_output/{opt['--trial']}/{opt['--machine']}", "pid": 4242,
                                  "topics": 19, "bytes": 1000, "growing": True, "stack_running": True,
                                  "other_recorders": [], "free_bytes": self.free, "total_bytes": 900 * GB, "t": self.t}), ""  # fmt: skip
        if verb == "check-topics":
            return 0, json.dumps({"ok": True, "topics": 19, "published": 19 - len(self.missing_topics),
                                  "missing": self.missing_topics}), ""  # fmt: skip
        if verb == "stop":
            if rec is None:
                return (
                    0,
                    json.dumps({"ok": True, "stopped": False, "was_recording": False}),
                    "",
                )
            if ip in self.stubborn and "--force" not in args:
                return (
                    4,
                    json.dumps(
                        {
                            "ok": False,
                            "recording": True,
                            "error": "the recorder (pid 4242) is still running after SIGINT",
                        }
                    ),
                    "",
                )
            del self.rec[ip]
            return 0, json.dumps({"ok": True, "stopped": True, "was_recording": True, "metadata": True,
                                  "size_bytes": 6 * GB, "duration_s": 120.0, "topics": 19,
                                  "silent_topics": self.silent, "free_bytes": self.free}), ""  # fmt: skip
        return 1, "", f"unexpected {args}"

    def run_parallel(self, fn, items, max_workers=8):
        return [(i, fn(i)) for i in items]

    def get_local_ips(self):
        return ["127.0.0.1"]

    def verbs(self, ip=None):
        return [a[0] for i, a in self.calls if ip in (None, i)]


@pytest.fixture
def rig(settings):
    client, svc = make_client(settings)
    fake = FakeRec()
    svc.helpers = fake
    t = svc.trials
    t.background = False
    t.sleep = lambda s: None
    now = [1_794_800_000.0]
    t.clock = lambda: now[0]
    ips = {n: svc.fleet.machine(n)["ip"] for n in ROBOTS}
    with client:
        yield type(
            "Rig",
            (),
            {"c": client, "svc": svc, "t": t, "fake": fake, "now": now, "ip": ips},
        )


def start(rig, description="Door search, two spots", machines=ROBOTS):
    return rig.c.post(
        "/api/trials/start", json={"description": description, "machines": machines}
    )


def events(rig, trial_id):
    path = (
        pathlib.Path(rig.svc.settings.output_root).expanduser()
        / trial_id
        / "console"
        / "events.jsonl"
    )
    return [json.loads(line) for line in path.read_text().splitlines()]


# --------------------------------------------------------------------- pure
def test_trial_id_and_slug():
    tid = tr.make_trial_id("Door search: two Spots, run #3!", 1_794_800_000.0)
    assert tid.startswith("trial_2026") and tid.endswith(
        "Z_door-search-two-spots-run-3"
    )
    assert tr.TRIAL_RE.match(tid)
    assert tr.slug("  ") == "trial"
    assert len(tr.slug("word " * 40)) <= 40


def test_recorder_command_quotes_but_keeps_tilde():
    cmd = tr.recorder_command(
        "~/dcist_ws/bin/adt4-rec",
        "~/adt4 output",
        "start",
        "--description",
        "a b; rm -rf /",
    )
    assert cmd.startswith(
        "python3 ~/dcist_ws/bin/adt4-rec --output-root ~/'adt4 output' start"
    )
    assert shlex.split(cmd)[-1] == "a b; rm -rf /"


def test_parse_reply_kinds():
    assert (
        tr.parse_reply(0, 'motd\n{"ok": true, "recording": false}')["recording"]
        is False
    )
    assert tr.parse_reply(255, "", "ssh: connect to host x: Connection timed out")[
        "unreachable"
    ]
    assert tr.parse_reply(-1, "", "SSH timeout")["unreachable"]
    assert tr.parse_reply(
        2, "", "python3: can't open file 'x': [Errno 2] No such file or directory"
    )["missing"]
    r = tr.parse_reply(1, "Traceback ...", "")
    assert not r["ok"] and "unreachable" not in r and "missing" not in r
    # a failing helper's own JSON wins over the exit status text
    assert (
        tr.parse_reply(3, '{"ok": false, "error": "already recording"}')["error"]
        == "already recording"
    )


def test_summarize_body_drops_bulk_and_secrets():
    body = json.dumps(
        {"message": "go to the door", "history": ["x"] * 999, "token": "s3cret"}
    ).encode()
    assert tr.summarize_body(body) == {"message": "go to the door"}
    assert tr.summarize_body(b"\x00\x01binary") == {"_bytes": 8}
    big = tr.summarize_body(json.dumps({"k": "v" * 9000}).encode())
    assert big["_chars"] > 9000 and len(big["_truncated"]) == 4000
    assert tr.summarize_body(b"") is None


def test_estimate():
    assert tr.estimate(60 * GB, 100e6) == pytest.approx(600)
    assert tr.estimate(60 * GB, 0) is None and tr.estimate(None, 5) is None


# ---------------------------------------------------------------- service
def test_start_records_on_every_machine_and_logs(rig):
    r = start(rig)
    assert r.status_code == 200, r.text
    a = r.json()["active"]
    assert a["state"] == "recording" and a["description"] == "Door search, two spots"
    assert sorted(a["machines"]) == ROBOTS
    assert all(m["state"] == "recording" for m in a["machines"].values())
    # every machine got the same trial id, its own name and its platform's topic list
    for name in ROBOTS:
        verbs = rig.fake.verbs(rig.ip[name])
        assert verbs == ["start", "check-topics"]
        args = next(a_ for ip, a_ in rig.fake.calls if ip == rig.ip[name])
        assert args[args.index("--trial") + 1] == a["trial_id"]
        assert args[args.index("--machine") + 1] == name
        assert args[args.index("--topic-set") + 1] == (
            "phoenix" if name == "gauss" else "spot"
        )
    ev = events(rig, a["trial_id"])
    assert [e["type"] for e in ev][:1] == ["trial_start"]
    assert {"trial_started", "machine_state"} <= {e["type"] for e in ev}
    manifest = json.loads((pathlib.Path(a["console_dir"]) / "trial.json").read_text())
    assert manifest["trial_id"] == a["trial_id"] and manifest["stopped"] is None
    # the hub carries the state for new clients
    assert rig.svc.hub.latest("trial/state")["active"]["trial_id"] == a["trial_id"]


def test_start_needs_a_description_and_refuses_a_second_trial(rig):
    assert start(rig, description="  ").status_code == 400
    assert rig.fake.calls == []
    assert start(rig).status_code == 200
    r = start(rig, description="another")
    assert r.status_code == 409 and "still recording" in r.json()["detail"]


def test_partial_start_is_visible_not_hidden(rig):
    rig.fake.unreachable.add(rig.ip["gauss"])
    rig.fake.fail_start[rig.ip["hamilton"]] = "topic list spot_topics.txt: no topics"
    a = start(rig).json()["active"]
    assert a["state"] == "recording"  # euclid records; the others say why not
    assert a["machines"]["euclid"]["state"] == "recording"
    assert (
        a["machines"]["gauss"]["state"] == "failed"
        and "no answer" in a["machines"]["gauss"]["message"]
    )
    assert "no topics" in a["machines"]["hamilton"]["message"]
    started = next(
        e for e in events(rig, a["trial_id"]) if e["type"] == "trial_started"
    )
    assert started["recording"] == 1 and started["not_recording"] == [
        "gauss",
        "hamilton",
    ]
    # retry one machine once it is back
    rig.fake.unreachable.clear()
    r = rig.c.post("/api/trials/machines/gauss/start")
    assert (
        r.status_code == 200
        and r.json()["active"]["machines"]["gauss"]["state"] == "recording"
    )
    assert rig.c.post("/api/trials/machines/euclid/start").status_code == 409


def test_nothing_started_ends_the_trial_as_failed(rig):
    for ip in rig.ip.values():
        rig.fake.missing.add(ip)
    body = start(rig).json()
    assert body["active"] is None and body["last"]["state"] == "failed"
    assert "not on this machine" in body["last"]["machines"]["euclid"]["message"]
    assert rig.c.post("/api/trials/flag", json={"text": "x"}).status_code == 409
    rig.fake.missing.clear()
    rig.now[0] += 1  # a new second: a new trial id
    assert start(rig).json()["active"]["state"] == "recording"  # a new start is allowed


def test_poll_measures_rate_and_disk_estimate(rig):
    start(rig)
    rig.now[0] += 10
    rig.t.poll_once()
    rig.now[0] += 10
    rig.t.poll_once()
    m = rig.c.get("/api/trials").json()["active"]["machines"]["euclid"]
    assert m["state"] == "recording" and m["rate_bps"] > 0
    # the fake's machine clock advances 0.5 s per call, 50 MB per status
    assert m["minutes_left"] == pytest.approx(200 * GB / m["rate_bps"] / 60, rel=0.01)
    assert m["clock_offset_s"] is not None and m["last_seen"] == rig.now[0]
    assert m["warnings"] == []


def test_low_disk_warns_once_and_never_stops(rig):
    start(rig)
    rig.fake.free = 4 * GB
    for _ in range(3):
        rig.now[0] += 10
        rig.t.poll_once()
    a = rig.c.get("/api/trials").json()["active"]
    assert a["state"] == "recording"
    assert all(m["state"] == "recording" for m in a["machines"].values())
    assert a["machines"]["euclid"]["warnings"][0].startswith("disk: 4.0 GB free")
    assert "stop" not in rig.fake.verbs()
    warn = [
        e
        for e in events(rig, a["trial_id"])
        if e["type"] == "warning" and e["machine"] == "euclid"
    ]
    assert len(warn) == 1


def test_a_bag_that_stops_growing_warns_unless_it_was_always_slow(rig):
    start(rig)
    for _ in range(3):  # 50 MB per poll: a camera bag
        rig.now[0] += 10
        rig.t.poll_once()
    rig.fake.rate = 0
    for _ in range(6):
        rig.now[0] += 10
        rig.t.poll_once()
    m = rig.c.get("/api/trials").json()["active"]["machines"]["euclid"]
    assert any(w.startswith("bag: has not grown") for w in m["warnings"])


def test_a_slow_bag_written_a_chunk_at_a_time_is_not_a_stall(rig):
    # a base station's bag: a few hundred kB every few minutes, as chunks fill
    rig.fake.rate = 0
    start(rig)
    for i in range(60):
        rig.fake.rate = 500_000 if i % 20 == 5 else 0
        rig.now[0] += 10
        rig.t.poll_once()
    a = rig.c.get("/api/trials").json()["active"]
    assert a["machines"]["euclid"]["bytes"] > 0
    assert a["machines"]["euclid"]["warnings"] == []
    assert not [e for e in events(rig, a["trial_id"]) if e["type"] == "warning"]


def test_unreachable_is_unknown_then_recovers(rig):
    start(rig)
    rig.now[0] += 10
    rig.t.poll_once()
    seen = rig.now[0]
    rig.fake.unreachable.add(rig.ip["hamilton"])
    rig.now[0] += 10
    rig.t.poll_once()
    m = rig.c.get("/api/trials").json()["active"]["machines"]["hamilton"]
    assert m["state"] == "unreachable" and m["last_seen"] == seen
    rig.fake.unreachable.clear()
    rig.now[0] += 10
    rig.t.poll_once()
    a = rig.c.get("/api/trials").json()["active"]
    assert a["machines"]["hamilton"]["state"] == "recording"
    states = [
        (e["machine"], e["state"])
        for e in events(rig, a["trial_id"])
        if e["type"] == "machine_state"
    ]
    assert ("hamilton", "unreachable") in states and states[-1] == (
        "hamilton",
        "recording",
    )


def test_recorder_that_died_shows_as_lost(rig):
    start(rig)
    del rig.fake.rec[rig.ip["euclid"]]  # stopped on the robot, or crashed
    rig.now[0] += 10
    rig.t.poll_once()
    m = rig.c.get("/api/trials").json()["active"]["machines"]["euclid"]
    assert m["state"] == "lost" and "NOT recording" in m["message"]


def test_topic_check_is_retried_until_the_machine_answers(rig):
    ip = rig.ip["hamilton"]
    real = rig.fake.ssh_cmd
    drop = {"n": 2}  # the first two check-topics to hamilton get no answer

    def flaky(user, addr, cmd, timeout=5):
        if addr == ip and " check-topics" in cmd and drop["n"] > 0:
            drop["n"] -= 1
            rig.fake.calls.append((addr, ["check-topics"]))
            return 255, "", "ssh: connect to host: Connection timed out"
        return real(user, addr, cmd, timeout=timeout)

    rig.fake.ssh_cmd = flaky
    rig.fake.missing_topics = ["/x/fix"]
    a = start(rig).json()["active"]
    tc = a["machines"]["hamilton"]["topic_check"]
    assert tc["ok"] is False and tc["retrying"] is True and tc["tries"] == 1
    assert a["machines"]["euclid"]["topic_check"]["ok"] is True
    rig.now[0] += 10
    rig.t.poll_once()  # second try: still no answer
    rig.now[0] += 10
    rig.t.poll_once()  # third try: it answers
    a = rig.c.get("/api/trials").json()["active"]
    tc = a["machines"]["hamilton"]["topic_check"]
    assert tc == {
        "ok": True,
        "tries": 3,
        "published": 18,
        "topics": 19,
        "missing": ["/x/fix"],
        "checked": rig.now[0],
        "tried": rig.now[0],
    }
    assert rig.fake.verbs(ip).count("check-topics") == 3
    rig.now[0] += 10
    rig.t.poll_once()  # done: not asked again until the next minute's repeat
    assert rig.fake.verbs(ip).count("check-topics") == 3
    assert rig.fake.verbs(rig.ip["euclid"]).count("check-topics") == 1
    kinds = [
        e["type"] for e in events(rig, a["trial_id"]) if e.get("machine") == "hamilton"
    ]
    assert "topic_check" in kinds and "topics_missing" in kinds


def test_topic_check_gives_up_loudly(rig):
    rig.fake.ssh_cmd_real = rig.fake.ssh_cmd

    def never(user, addr, cmd, timeout=5):
        if " check-topics" in cmd:
            return 255, "", "ssh: connect to host: Connection timed out"
        return rig.fake.ssh_cmd_real(user, addr, cmd, timeout=timeout)

    rig.fake.ssh_cmd = never
    a = start(rig).json()["active"]
    for _ in range(tr.CHECK_TRIES + 2):
        rig.now[0] += 10
        rig.t.poll_once()
    tc = rig.c.get("/api/trials").json()["active"]["machines"]["euclid"]["topic_check"]
    assert (
        tc["ok"] is False and tc["tries"] == tr.CHECK_TRIES and tc["retrying"] is False
    )
    failed = [
        e for e in events(rig, a["trial_id"]) if e["type"] == "topic_check_failed"
    ]
    assert len(failed) == 3


def test_flags_are_timestamped_and_only_during_a_trial(rig):
    assert rig.c.post("/api/trials/flag", json={"text": "early"}).status_code == 409
    a = start(rig).json()["active"]
    rig.now[0] += 75.5
    r = rig.c.post(
        "/api/trials/flag", json={"text": "  hamilton hesitated at the door  "}
    )
    assert r.status_code == 200
    ev = r.json()["event"]
    assert ev["text"] == "hamilton hesitated at the door" and ev["elapsed_s"] == 75.5
    assert ev["t"] == rig.now[0] and ev["iso"].endswith("Z")
    assert rig.c.post("/api/trials/flag", json={"text": " "}).status_code == 400
    flags = [e for e in events(rig, a["trial_id"]) if e["type"] == "flag"]
    assert len(flags) == 1
    assert rig.c.get("/api/trials").json()["active"]["flags"] == 1
    tail = rig.c.get("/api/trials/events?n=1").json()["events"]
    assert tail[-1]["type"] == "flag"
    # a flag costs no ssh
    assert "flag" not in " ".join(rig.fake.verbs())


def test_stop_closes_every_bag_and_reports_silent_topics(rig):
    a = start(rig).json()["active"]
    rig.now[0] += 120
    body = rig.c.post("/api/trials/stop").json()
    assert body["active"] is None
    last = body["last"]
    assert last["state"] == "stopped" and last["stopped"] == rig.now[0]
    for name in ROBOTS:
        m = last["machines"][name]
        assert m["state"] == "stopped" and m["stop"]["metadata"] is True
        assert m["warnings"] == ["1 of 19 topics recorded no message"]
    ev = events(rig, a["trial_id"])
    assert ev[-1]["type"] == "trial_end" and ev[-1]["duration_s"] == 120
    manifest = json.loads((pathlib.Path(a["console_dir"]) / "trial.json").read_text())
    assert (
        manifest["stopped_at"]
        and manifest["machines"]["gauss"]["stop"]["size_bytes"] == 6 * GB
    )
    # the measured rate feeds the next preflight's estimate
    pre = {m["name"]: m for m in rig.c.get("/api/trials/preflight").json()["machines"]}
    assert pre["euclid"]["rate_bps"] == pytest.approx(6 * GB / 120)
    assert pre["euclid"]["minutes"] == pytest.approx(
        200 * GB / (6 * GB / 120) / 60, abs=0.1
    )
    assert pre["euclid"]["ready"] is True
    assert rig.c.post("/api/trials/stop").status_code == 409


def test_stop_with_a_robot_out_of_range_keeps_trying(rig):
    start(rig)
    rig.fake.unreachable.add(rig.ip["gauss"])
    body = rig.c.post("/api/trials/stop").json()
    a = body["active"]
    assert a is not None and a["state"] == "stopping"
    assert a["machines"]["gauss"]["state"] == "stop_pending"
    assert a["machines"]["euclid"]["state"] == "stopped"
    assert rig.fake.rec.get(rig.ip["gauss"])  # it is still recording out there
    rig.t.poll_once()  # still out of range
    assert (
        rig.c.get("/api/trials").json()["active"]["machines"]["gauss"]["state"]
        == "stop_pending"
    )
    rig.fake.unreachable.clear()
    rig.t.poll_once()  # back: the stop goes through
    body = rig.c.get("/api/trials").json()
    assert (
        body["active"] is None
        and body["last"]["machines"]["gauss"]["state"] == "stopped"
    )


def test_stubborn_recorder_needs_force_or_close(rig):
    start(rig)
    rig.fake.stubborn.add(rig.ip["euclid"])
    a = rig.c.post("/api/trials/stop").json()["active"]
    assert a["machines"]["euclid"]["state"] == "stop_failed"
    assert (
        rig.c.post("/api/trials/start", json={"description": "next"}).status_code == 409
    )
    body = rig.c.post("/api/trials/stop", json={"force": True}).json()
    assert (
        body["active"] is None
        and body["last"]["machines"]["euclid"]["state"] == "stopped"
    )


def test_close_ends_a_trial_and_names_what_was_left(rig):
    start(rig)
    assert rig.c.post("/api/trials/close").status_code == 409  # stop first
    rig.fake.unreachable.add(rig.ip["gauss"])
    rig.c.post("/api/trials/stop")
    body = rig.c.post("/api/trials/close").json()
    assert body["active"] is None
    assert body["last"]["note"] == "closed with gauss not confirmed stopped"
    assert body["last"]["machines"]["gauss"]["state"] == "stop_pending"


def test_restore_picks_up_an_unfinished_trial(rig):
    a = start(rig).json()["active"]
    again = tr.TrialService(
        rig.svc.settings, rig.svc.fleet, lambda: rig.fake, None, background=False
    )
    again.restore()
    assert again.active["trial_id"] == a["trial_id"]
    again.poll_once()
    assert again.active["machines"]["euclid"]["state"] == "recording"
    assert any(e["type"] == "console_restarted" for e in events(rig, a["trial_id"]))


def test_preflight_reports_missing_helper_and_running_recorder(rig):
    rig.fake.missing.add(rig.ip["gauss"])
    rig.fake.rec[rig.ip["hamilton"]] = {"trial": "trial_old", "bytes": 5}
    pre = {m["name"]: m for m in rig.c.get("/api/trials/preflight").json()["machines"]}
    assert (
        pre["gauss"]["ready"] is False
        and "not on this machine" in pre["gauss"]["error"]
    )
    assert (
        pre["hamilton"]["ready"] is False
        and "already recording trial_old" in pre["hamilton"]["notes"][0]
    )
    assert (
        pre["euclid"]["ready"] is True and pre["euclid"]["minutes"] is None
    )  # no rate known yet


def test_this_machine_records_by_default_and_gets_every_robots_plan(rig):
    with rig.svc.fleet.lock:
        rig.svc.fleet.runtime_config["willow"]["local"] = True
    # this machine's helper runs locally, not over ssh
    rig.t._run_local = lambda cmd, timeout: rig.fake.ssh_cmd(
        "me", "local", cmd, timeout=int(timeout)
    )
    a = start(rig, machines=None).json()[
        "active"
    ]  # the default: robots and this machine
    assert "willow" in a["machines"] and "euclid" in a["machines"]
    args = next(x for ip, x in rig.fake.calls if x[0] == "start" and "willow" in x)
    assert args[args.index("--topic-set") + 1] == "base_station"
    robots = args[args.index("--robots") + 1].split(",")
    assert {"euclid", "hamilton", "gauss"} <= set(robots) and "willow" not in robots
    pre = rig.c.get("/api/trials/preflight").json()
    assert pre["base_station"] == "willow" and pre["base_note"] is None
    assert next(m for m in pre["machines"] if m["name"] == "willow")["local"] is True
    rig.c.post("/api/trials/stop")
    rig.now[0] += 1
    # unticked: it is not recorded
    b = start(rig, machines=["euclid"]).json()["active"]
    assert sorted(b["machines"]) == ["euclid"]


def test_preflight_says_when_there_is_no_base_station_bag(rig):
    pre = rig.c.get("/api/trials/preflight").json()
    assert pre["base_station"] is None and "no base-station bag" in pre["base_note"]


def test_context_is_saved_at_start_and_stop(rig):
    state = {
        "map": {"name": "b45_v3", "dsg_sha256": "aa"},
        "launch": {"experiment": "nov15"},
    }
    rig.t.context = lambda: json.loads(json.dumps(state))
    a = start(rig).json()["active"]
    assert a["context_start"]["map"]["name"] == "b45_v3"
    state["map"] = {
        "name": "b45_v4",
        "dsg_sha256": "bb",
    }  # the map was switched mid-trial
    last = rig.c.post("/api/trials/stop").json()["last"]
    assert last["context_start"]["map"]["dsg_sha256"] == "aa"
    assert last["context_stop"]["map"]["dsg_sha256"] == "bb"
    ctx = [e for e in events(rig, a["trial_id"]) if e["type"] == "context"]
    assert [e["when"] for e in ctx] == ["start", "stop"]


def test_a_failing_context_does_not_stop_the_trial(rig):
    def boom():
        raise RuntimeError("neo4j is down")

    rig.t.context = boom
    a = start(rig).json()["active"]
    assert a["state"] == "recording" and "neo4j is down" in a["context_start"]["error"]


def test_default_console_context_parts_fail_on_their_own(rig):
    from adt4_console.app import trial_context

    ctx = trial_context(rig.svc)
    assert set(ctx) == {"launch", "map", "missions", "ros"}
    assert ctx["launch"] is None and ctx["ros"] is False


def test_hub_state_is_logged_while_recording_deduped(rig):
    rig.svc.hub.publish("manipulation/pending", [{"id": "before"}])  # no trial: nothing
    a = start(rig).json()["active"]
    rig.svc.hub.publish(
        "manipulation/pending", [{"id": "r1", "robot": "euclid", "kind": "pick"}]
    )
    rig.now[0] += 1
    rig.svc.hub.publish(
        "manipulation/pending", [{"id": "r1", "robot": "euclid", "kind": "pick"}]
    )  # same
    rig.svc.hub.publish("robot/euclid/state", {"x": 1})  # not a logged topic
    rig.now[0] += 1
    rig.svc.hub.publish("manipulation/pending", [])
    rig.svc.hub.publish("mission/m1", {"plan": ["x" * 50_000]})  # cut, not dropped
    rig.t._drain_hub()
    hub = [e for e in events(rig, a["trial_id"]) if e["type"] == "hub"]
    assert [(e["topic"], e["data"]) for e in hub[:2]] == [
        ("manipulation/pending", [{"id": "r1", "robot": "euclid", "kind": "pick"}]),
        ("manipulation/pending", []),
    ]
    assert hub[2]["topic"] == "mission/m1" and hub[2]["data"]["_chars"] > 50_000


def test_screen_recording_chunks_append_in_order(rig):
    q = "/api/trials/screen?session=s1&mime=video/webm;codecs=vp9"
    assert rig.c.post(q + "&seq=0", content=b"AAAA").status_code == 409  # no trial
    a = start(rig).json()["active"]
    assert (
        rig.c.post("/api/trials/screen?session=../x&seq=0", content=b"x").status_code
        == 400
    )
    r = rig.c.post(q + "&seq=0&t_client=12.5", content=b"AAAA").json()
    assert r == {"ok": True, "next": 1, "bytes": 4, "file": "screen_s1.webm"}
    assert (
        rig.c.post(q + "&seq=0", content=b"AAAA").json()["duplicate"] is True
    )  # a retry
    rig.c.post(q + "&seq=1", content=b"BB")
    video = pathlib.Path(a["console_dir"]) / "screen_s1.webm"
    assert video.read_bytes() == b"AAAABB"
    rig.now[0] += 60
    rig.c.post("/api/trials/stop")
    # the last piece arrives after the trial ended, and is still taken
    rig.now[0] += 5
    r = rig.c.post(q + "&seq=2&final=true", content=b"C")
    assert r.status_code == 200 and video.read_bytes() == b"AAAABBC"
    last = rig.c.get("/api/trials").json()["last"]
    st = last["screen"]["s1"]
    assert st["chunks"] == 3 and st["bytes"] == 7 and st["ended"] and st["gaps"] == 0
    kinds = [
        e["type"] for e in events(rig, a["trial_id"]) if e["type"].startswith("screen")
    ]
    assert kinds == ["screen_start", "screen_stop"]
    # a session the trial never had, or one that comes far too late, is refused
    assert (
        rig.c.post("/api/trials/screen?session=other&seq=0", content=b"x").status_code
        == 409
    )
    rig.now[0] += 500
    assert rig.c.post(q + "&seq=3", content=b"x").status_code == 409
    manifest = json.loads((pathlib.Path(a["console_dir"]) / "trial.json").read_text())
    assert manifest["screen"]["s1"]["bytes"] == 7


def test_screen_note_is_logged(rig):
    a = start(rig).json()["active"]
    rig.c.post(
        "/api/trials/screen/note",
        json={"session": "s1", "what": "denied", "detail": "NotAllowedError"},
    )
    ev = [e for e in events(rig, a["trial_id"]) if e["type"] == "screen_note"]
    assert ev[0]["what"] == "denied"


def test_ui_events_are_logged_only_while_recording(rig):
    body = {"events": [{"kind": "click", "label": "Approve", "detail": "in dialog: Approve pick?",
                        "page": "/viewer", "t_client": 1.5, "evil": {"x": 1}},
                       {"label": "no kind"}, "junk"]}  # fmt: skip
    assert rig.c.post("/api/trials/ui", json=body).json() == {"logged": 0}
    a = start(rig).json()["active"]
    assert rig.c.post("/api/trials/ui", json=body).json() == {"logged": 1}
    ui = [e for e in events(rig, a["trial_id"]) if e["type"] == "ui"]
    assert len(ui) == 1 and ui[0]["label"] == "Approve" and ui[0]["page"] == "/viewer"
    assert ui[0]["action"] == "click"
    assert "evil" not in ui[0]
    # and the ui post itself is not logged again as a request
    assert not [e for e in events(rig, a["trial_id"]) if e["type"] == "request"]


def test_request_categories():
    cat = tr.request_category
    assert cat("/api/mission/plan/preview_pddl") == "preview"
    assert cat("/api/edit/live/preview") == "preview"
    assert cat("/api/launch/dry_run") == "dry_run"
    assert cat("/api/jobs/launch-3-1794800000/confirm") == "job_confirm"
    assert cat("/api/jobs/launch-3-1794800000/cancel") == "job_cancel"
    assert cat("/api/mission/m1/execute") == "action"
    assert cat("/api/robots/euclid/goal/pddl") == "action"


def test_job_confirmation_is_logged_with_its_question(rig):
    import threading

    a = start(rig).json()["active"]
    got = {}

    def work(job):
        got["answer"] = job.confirm("The recorder on euclid is still running. Stop harder?",
                                    kind="stop_bag_harder", yes_label="Stop harder", no_label="Leave it")  # fmt: skip

    job = rig.svc.jobs.start("stop_bag", "Stop bag on euclid", work)
    for _ in range(200):
        if job.confirm_request:
            break
        threading.Event().wait(0.01)
    r = rig.c.post(f"/api/jobs/{job.id}/confirm", json={"answer": False})
    assert r.status_code == 200
    rig.svc.jobs.wait(job.id, 5)
    assert got["answer"] is False
    ev = [e for e in events(rig, a["trial_id"]) if e["type"] == "request"][-1]
    assert ev["category"] == "job_confirm" and ev["body"]["answer"] is False
    assert ev["job"]["title"] == "Stop bag on euclid"
    assert ev["job"]["question"].startswith("The recorder on euclid is still running")
    assert ev["job"]["no_label"] == "Leave it"


def test_plan_requests_keep_their_response_and_previews_are_logged(rig):
    a = start(rig).json()["active"]
    r = rig.c.post("/api/mission/plan/preview_pddl", json={"goal": "(and)"})
    reqs = [e for e in events(rig, a["trial_id"]) if e["type"] == "request"]
    assert [(e["path"], e["status"]) for e in reqs] == [
        ("/api/mission/plan/preview_pddl", r.status_code)
    ]
    assert reqs[0]["body"] == {"goal": "(and)"} and "response" in reqs[0]
    assert reqs[0]["category"] == "preview"
    assert tr.wants_response("/api/robots/euclid/goal/pddl") and tr.wants_response(
        "/api/edit/live/nodes"
    )
    assert not tr.wants_response("/api/fleet/status/refresh")


# ------------------------------------------------------------ request log
def test_operator_requests_are_logged_only_while_recording(rig):
    rig.c.post("/api/fleet/status/refresh")  # before: not logged anywhere
    a = start(rig).json()["active"]
    r = rig.c.post("/api/fleet/status/refresh")
    rig.c.get("/api/fleet")  # reads are not logged
    rig.c.post(
        "/api/robots/euclid/teleop/cmd_vel", json={"vx": 0.1}
    )  # too many: skipped
    rig.c.post("/api/trials/flag", json={"text": "f"})  # logged as a flag, not twice
    reqs = [e for e in events(rig, a["trial_id"]) if e["type"] == "request"]
    assert [(e["method"], e["path"], e["status"]) for e in reqs] == [
        ("POST", "/api/fleet/status/refresh", r.status_code)]  # fmt: skip


def test_request_log_failure_never_breaks_the_request(rig, monkeypatch):
    start(rig)

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(rig.t, "log_request", boom)
    assert rig.c.post("/api/fleet/status/refresh").status_code < 500


def test_the_local_helper_runs_in_its_own_session():
    rc, out, _ = tr._run_local("python3 -c 'import os; print(os.getsid(0))'", 5)
    assert rc == 0 and int(out) != os.getsid(0)


def test_a_machine_is_removed_and_added_while_the_trial_records(rig):
    a = start(rig, machines=["euclid", "gauss"]).json()["active"]
    assert sorted(a["machines"]) == ["euclid", "gauss"]
    # remove gauss: its bag closes, the trial and euclid go on
    body = rig.c.post("/api/trials/machines/gauss/stop").json()
    a = body["active"]
    assert a["state"] == "recording"
    g = a["machines"]["gauss"]
    assert g["state"] == "stopped" and g["removed"] is True
    assert rig.fake.rec.get(rig.ip["gauss"]) is None
    assert a["machines"]["euclid"]["state"] == "recording"
    assert rig.c.post("/api/trials/machines/gauss/stop").status_code == 409
    assert rig.c.post("/api/trials/machines/nobody/stop").status_code == 404
    rig.t.poll_once()  # a removed machine does not end the trial or get polled
    assert rig.c.get("/api/trials").json()["active"]["state"] == "recording"
    # add it back, and add one that was never in the trial
    g = rig.c.post("/api/trials/machines/gauss/start").json()["active"]["machines"][
        "gauss"
    ]
    assert g["state"] == "recording" and g["removed"] is False
    h = rig.c.post("/api/trials/machines/hamilton/start").json()["active"]["machines"]
    assert h["hamilton"]["state"] == "recording"
    kinds = [(e["type"], e.get("machine")) for e in events(rig, a["trial_id"])]
    assert ("machine_remove", "gauss") in kinds
    assert ("machine_add", "gauss") in kinds and ("machine_add", "hamilton") in kinds
    # the trial's stop closes all three
    last = rig.c.post("/api/trials/stop").json()["last"]
    assert {n: m["state"] for n, m in last["machines"].items()} == {
        "euclid": "stopped", "gauss": "stopped", "hamilton": "stopped"}  # fmt: skip


def test_removing_a_machine_out_of_range_is_retried(rig):
    start(rig)
    rig.fake.unreachable.add(rig.ip["gauss"])
    a = rig.c.post("/api/trials/machines/gauss/stop").json()["active"]
    assert a["state"] == "recording"
    assert a["machines"]["gauss"]["state"] == "stop_pending"
    rig.fake.unreachable.clear()
    rig.t.poll_once()
    a = rig.c.get("/api/trials").json()["active"]
    assert a["state"] == "recording" and a["machines"]["gauss"]["state"] == "stopped"


def test_stop_pressed_while_the_machines_start_leaves_no_recorder_behind(rig):
    real = rig.fake.ssh_cmd
    pressed = []

    def stop_during_first_start(user, addr, cmd, timeout=5):
        if " start " in cmd and not pressed:
            pressed.append(addr)
            body = rig.c.post("/api/trials/stop").json()
            # the trial stays open: machines are still starting
            assert body["active"] is not None and body["active"]["state"] == "stopping"
        return real(user, addr, cmd, timeout=timeout)

    rig.fake.ssh_cmd = stop_during_first_start
    body = start(rig).json()
    assert pressed and rig.fake.rec == {}  # every recorder that came up was stopped
    assert body["active"] is None
    last = body["last"]
    assert last["state"] == "stopped"
    assert {m["state"] for m in last["machines"].values()} == {"stopped"}
    for ip in rig.ip.values():
        assert rig.fake.verbs(ip)[-1] == "stop"


def test_start_replaces_a_recorder_left_running_only_when_asked(rig):
    stray = {"trial": "trial_old", "bytes": 5}
    rig.fake.rec[rig.ip["hamilton"]] = dict(stray)
    rig.fake.rec[rig.ip["gauss"]] = dict(stray)
    pre = {m["name"]: m for m in rig.c.get("/api/trials/preflight").json()["machines"]}
    assert pre["hamilton"]["ready"] is False and pre["hamilton"]["replaceable"] is True
    assert pre["hamilton"]["recording"] == "trial_old"
    assert pre["euclid"]["replaceable"] is False and pre["euclid"]["recording"] is None
    r = rig.c.post(
        "/api/trials/start",
        json={"description": "door", "machines": ROBOTS, "replace": ["hamilton"]},
    )
    a = r.json()["active"]
    # hamilton: the old recording was closed and this trial started
    h = a["machines"]["hamilton"]
    assert h["state"] == "recording" and "trial_old" in h["warnings"][0]
    assert rig.fake.verbs(rig.ip["hamilton"])[:4] == [
        "status",
        "start",
        "stop",
        "start",
    ]
    assert rig.fake.rec[rig.ip["hamilton"]]["trial"] == a["trial_id"]
    # gauss was not named: its recorder is left alone
    g = a["machines"]["gauss"]
    assert g["state"] == "failed" and "already recording" in g["message"]
    assert rig.fake.rec[rig.ip["gauss"]]["trial"] == "trial_old"
    assert any(
        e["type"] == "recorder_replaced" and e["machine"] == "hamilton"
        for e in events(rig, a["trial_id"])
    )
    # the chip's "Stop that and record" for the one that failed
    r = rig.c.post("/api/trials/machines/gauss/start", json={"replace": True})
    assert r.json()["active"]["machines"]["gauss"]["state"] == "recording"
    assert rig.fake.rec[rig.ip["gauss"]]["trial"] == a["trial_id"]


def test_a_stray_recorder_is_stopped_from_the_console(rig):
    rig.fake.rec[rig.ip["gauss"]] = {"trial": "trial_old", "bytes": 5}
    r = rig.c.post("/api/trials/machines/gauss/stop-recorder")
    assert r.status_code == 200 and r.json()["was_recording"] is True
    assert rig.fake.rec.get(rig.ip["gauss"]) is None
    assert rig.c.get("/api/trials/preflight").json()["machines"][1]["name"] == "gauss"
    pre = {m["name"]: m for m in rig.c.get("/api/trials/preflight").json()["machines"]}
    assert pre["gauss"]["ready"] is True
    # never a machine that records for the active trial
    start(rig)
    r = rig.c.post("/api/trials/machines/gauss/stop-recorder")
    assert r.status_code == 409 and rig.fake.rec.get(rig.ip["gauss"])
    rig.fake.unreachable.add(rig.ip["euclid"])
    rig.c.post("/api/trials/stop")


def test_the_topic_check_is_repeated_and_follows_a_restarted_stack(rig):
    rig.fake.missing_topics = ["/x/lidar", "/x/fix"]
    a = start(rig, machines=["euclid"]).json()["active"]
    ip = rig.ip["euclid"]
    tc = a["machines"]["euclid"]["topic_check"]
    assert tc["missing"] == ["/x/lidar", "/x/fix"] and tc["checked"] == rig.now[0]
    rig.now[0] += 30
    rig.t.poll_once()  # not yet: one check a minute
    assert rig.fake.verbs(ip).count("check-topics") == 1
    # the stack is restarted and the lidar comes up
    rig.fake.missing_topics = ["/x/fix"]
    rig.now[0] += 31
    rig.t.poll_once()
    tc = rig.c.get("/api/trials").json()["active"]["machines"]["euclid"]["topic_check"]
    assert tc["ok"] and tc["missing"] == ["/x/fix"] and tc["published"] == 18
    assert tc["checked"] == rig.now[0]
    assert rig.fake.verbs(ip).count("check-topics") == 2
    # a repeat with the same answer adds nothing to the log
    rig.now[0] += 61
    rig.t.poll_once()
    ev = [e for e in events(rig, a["trial_id"]) if e["type"].startswith("topic")]
    assert [e["type"] for e in ev] == [
        "topic_check",
        "topics_missing",
        "topics_changed",
    ]
    assert ev[-1]["appeared"] == ["/x/lidar"] and ev[-1]["gone"] == []
    # a repeat that gets no answer keeps the last one, and waits an interval
    good = rig.now[0]
    real = rig.fake.ssh_cmd

    def no_check(user, addr, cmd, timeout=5):
        if " check-topics" in cmd:
            rig.fake.calls.append((addr, ["check-topics"]))
            return 255, "", "ssh: connect to host: Connection timed out"
        return real(user, addr, cmd, timeout)

    rig.fake.ssh_cmd = no_check
    rig.now[0] += 61
    rig.t.poll_once()
    rig.t.poll_once()
    tc = rig.c.get("/api/trials").json()["active"]["machines"]["euclid"]["topic_check"]
    assert tc["ok"] and tc["missing"] == ["/x/fix"] and tc["checked"] == good
    assert "timed out" in tc["recheck_error"] and tc["tried"] == rig.now[0]
    assert rig.fake.verbs(ip).count("check-topics") == 4


def test_the_topic_recheck_can_be_switched_off(rig):
    rig.t.settings.trial_topic_recheck_s = 0
    start(rig, machines=["euclid"])
    rig.now[0] += 600
    rig.t.poll_once()
    assert rig.fake.verbs(rig.ip["euclid"]).count("check-topics") == 1
