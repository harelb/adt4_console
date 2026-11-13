"""Rosbag recording detection (the on-robot probe + its parsing) and the clean
stop: Ctrl-C to the recorder's pane only, wait, report, stop harder on request."""

import json
import os
import subprocess
import sys
import time

import pytest

from adt4_console import recording
from adt4_console import recording_probe as rp
from adt4_console.fleet_service import FleetService

from .conftest import fake_fleet_deps, make_client

ROS2 = "/opt/ros/jazzy/bin/ros2"


def _procs():
    """adt4_system: pane %3 (zsh 100) -> zsh -ic -> ros2 run record_topics ->
    ros2 bag record (pid 104).  Pane %4 (zsh 200) runs hydra.  A rosbag2 child
    of the recorder is not a second recording."""
    rec_argv = [
        "/usr/bin/python3",
        ROS2,
        "bag",
        "record",
        "-o",
        "out/recorded_data",
        "--topics",
        "/a",
    ]
    return {
        1: {"ppid": 0, "argv": ["/sbin/init"]},
        100: {"ppid": 1, "argv": ["-zsh"]},
        101: {
            "ppid": 100,
            "argv": ["zsh", "-ic", "ros2 run dcist_launch_system record_topics x y"],
        },
        102: {
            "ppid": 101,
            "argv": [
                "/usr/bin/python3",
                ROS2,
                "run",
                "dcist_launch_system",
                "record_topics",
            ],
        },
        103: {"ppid": 102, "argv": ["/usr/bin/python3", "record_topics", "x", "y"]},
        104: {
            "ppid": 103,
            "argv": rec_argv,
            "cwd": "/home/swarm/adt4_output/run1",
            "started": 1700000000.0,
        },
        105: {
            "ppid": 104,
            "argv": ["/opt/ros/jazzy/lib/rosbag2_transport/rosbag2_recorder", "record"],
        },
        200: {"ppid": 1, "argv": ["-zsh"]},
        201: {"ppid": 200, "argv": ["hydra_ros_node", "--bag", "record"]},
        # the console's own probe ssh: "ros2 bag record" in a zsh -c string is not argv
        300: {"ppid": 1, "argv": ["zsh", "-l", "-c", "pgrep -f 'ros2 bag record'"]},
    }


PANES = (
    "%3\t100\tadt4_system\trecording\t7.0\n"
    "%4\t200\tadt4_system\thydra\t2.0\n"
    "garbage line\n"
)


def test_probe_scan_finds_the_recorder_and_its_pane():
    panes = rp.parse_panes(PANES)
    assert set(panes) == {100, 200}
    recs = rp.scan(_procs(), panes)
    assert recs == [
        {
            "pid": 104,
            "pane": "%3",
            "target": "adt4_system:7.0",
            "session": "adt4_system",
            "window": "recording",
            "output": "/home/swarm/adt4_output/run1/out/recorded_data",
            "started": 1700000000.0,
        }
    ]


def test_probe_scan_other_session_no_pane_and_open_fd_output():
    procs = {
        1: {"ppid": 0, "argv": ["/sbin/init"]},
        50: {"ppid": 1, "argv": ["bash"]},
        51: {
            "ppid": 50,
            "argv": [
                "/usr/bin/python3",
                ROS2,
                "bag",
                "record",
                "--output=/data/b1",
                "-a",
            ],
        },
        60: {
            "ppid": 1,
            "argv": ["/usr/bin/python3", ROS2, "bag", "record", "-a"],
            "fds": ["/dev/null"],
        },
        61: {
            "ppid": 60,
            "argv": ["helper"],
            "fds": ["/home/u/rosbag2_2026/rosbag2_2026_0.mcap"],
        },
    }
    recs = rp.scan(procs, rp.parse_panes("%9\t50\tmybag\tw\t0.0"))
    assert [(r["pid"], r["pane"], r["target"], r["output"]) for r in recs] == [
        (51, "%9", "mybag:0.0", "/data/b1"),
        (60, None, None, "/home/u/rosbag2_2026"),
    ]
    assert rp.scan({}, {}) == []


def test_parse_probe_and_summarize():
    recs = [
        {"pid": 9, "pane": None, "output": "/x", "started": 1.0, "size_bytes": 5},
        {
            "pid": 104,
            "pane": "%3",
            "target": "adt4_system:7.0",
            "output": "/b",
            "started": 2.0,
            "size_bytes": 2048,
            "metadata": False,
        },
    ]
    out = "motd noise\n---REC---\n" + json.dumps({"recordings": recs, "t": 3.0}) + "\n"
    assert recording.parse_probe(out) == recs
    s = recording.summarize(recording.parse_probe(out))
    # the pane-owned recording is the one the card stops
    assert {k: s[k] for k in ("active", "pane", "output", "started", "size_bytes", "pid", "count")} == {
        "active": True, "pane": "%3", "output": "/b", "started": 2.0, "size_bytes": 2048, "pid": 104, "count": 2,
    }  # fmt: skip
    assert recording.summarize([]) == recording.NOT_RECORDING
    assert recording.summarize([])["active"] is False
    assert recording.parse_probe("") is None
    assert recording.parse_probe("---REC---\nTraceback ...") is None
    assert recording.summarize(None)[
        "active"
    ] is False and "error" in recording.summarize(None)


def test_probe_command_is_valid_shell_and_carries_the_probe():
    cmd = recording.probe_command()
    assert subprocess.run(["bash", "-n", "-c", cmd]).returncode == 0
    assert cmd.startswith("echo '---REC---'") and cmd.endswith("| python3 -")


def test_probe_runs_for_real_on_a_fake_recorder(tmp_path):
    """The probe as sent: finds a process named ``ros2`` running ``bag record``."""
    fake = tmp_path / "bin" / "ros2"
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\nmkdir -p "$4"; echo hi > "$4/b_0.mcap"; sleep 30\n')
    fake.chmod(0o755)
    out_dir = tmp_path / "bags" / "recorded_data"
    p = subprocess.Popen(
        ["sh", str(fake), "bag", "record", "-o", str(out_dir), "--topics", "/a"],
        start_new_session=True,
    )
    try:
        for _ in range(50):
            if (out_dir / "b_0.mcap").exists():
                break
            time.sleep(0.05)
        r = subprocess.run(
            ["bash", "-c", recording.probe_command()],
            capture_output=True,
            text=True,
            timeout=30,
        )
        recs = recording.parse_probe(r.stdout)
        mine = [x for x in recs or [] if x["output"] == str(out_dir)]
        assert len(mine) == 1, (r.stdout, r.stderr)
        assert mine[0]["size_bytes"] == 3 and mine[0]["metadata"] is False
        assert mine[0]["started"] and abs(mine[0]["started"] - time.time()) < 120
    finally:
        os.killpg(p.pid, 9)
        p.wait()


# --------------------------------------------------------------- the poll
def test_status_poll_adds_recording_for_robots_with_tmux(settings):
    calls = []
    rec = [{"pid": 104, "pane": "%3", "output": "/b", "started": 2.0, "size_bytes": 10}]

    def probe(user, ip):
        calls.append(ip)
        return rec

    deps = fake_fleet_deps(
        get_remote_status=lambda user, ip, **_: {"tmux_sessions": "adt4_system"},
        probe_recording=probe,
        run_parallel=lambda fn, ms: [(m, fn(m)) for m in ms],
    )
    fleet = FleetService(settings, deps=deps)
    fleet.refresh_reachability()
    status = fleet.refresh_status()["machines"]
    robots = {n for n, m in fleet.runtime_config.items() if m.get("role") == "robot"}
    assert (
        robots
        and robots < set(status)
        and all(status[r]["recording"]["active"] for r in robots)
    )
    assert status[next(iter(robots))]["recording"]["pane"] == "%3"
    # one probe per robot per poll, none for base stations
    assert len(calls) == len(robots)
    assert all("recording" not in v for k, v in status.items() if k not in robots)

    calls.clear()
    deps.get_remote_status = lambda user, ip, **_: {"tmux_sessions": "none"}
    status = fleet.refresh_status()["machines"]
    assert calls == [] and not any(status[r]["recording"]["active"] for r in robots)


# --------------------------------------------------------------- the stop
class RobotSim:
    """ssh_cmd fake for one robot: two panes, a recorder in %3 that exits
    after ``exit_after`` Ctrl-Cs (None: never, unless ``term_exits``)."""

    def __init__(self, exit_after=1, alive_polls=1, term_exits=True, metadata=True):
        self.exit_after, self.alive_polls = exit_after, alive_polls
        self.term_exits, self.metadata = term_exits, metadata
        self.cmds, self.ctrl_c, self.termed, self.polls = [], 0, False, 0

    def gone(self):
        return (self.exit_after is not None and self.ctrl_c >= self.exit_after) or (
            self.termed and self.term_exits
        )

    def ssh_cmd(self, user, ip, cmd, timeout=5):
        self.cmds.append(cmd)
        if "---REC---" in cmd:
            recs = [] if self.gone() else [
                {"pid": 104, "pane": "%3", "target": "adt4_system:7.0", "output": "/home/swarm/run1/recorded_data", "started": 1.0},
            ]  # fmt: skip
            return 0, "---REC---\n" + json.dumps({"recordings": recs}), ""
        if cmd.startswith("tmux send-keys"):
            self.ctrl_c += 1
            return 0, "", ""
        if "kill -0" in cmd:
            self.polls += 1
            return (
                0,
                "GONE" if self.gone() and self.polls > self.alive_polls else "RUNNING",
                "",
            )
        if cmd.startswith("kill -TERM"):
            self.termed = True
            return 0, "", ""
        if cmd.startswith("du -sb"):
            return (
                0,
                "123456789\n"
                + ("METADATA_OK" if self.metadata else "METADATA_MISSING"),
                "",
            )
        raise AssertionError(f"unexpected remote command {cmd!r}")


class FakeJob:
    def __init__(self, answer=None):
        self.lines, self.answer, self.confirms = [], answer, []

    def log(self, level, text):
        self.lines.append((level, text))

    def check_cancelled(self):
        pass

    def confirm(self, message, **kw):
        self.confirms.append((message, kw))
        return self.answer


def _stop(sim, job=None, **kw):
    job = job or FakeJob()
    res = recording.stop_recording(sim.ssh_cmd, "swarm", "203.0.113.2", "hamilton", job,
                                   wait_s=kw.pop("wait_s", 1.0), harder_wait_s=0.2, poll_s=0.01, **kw)  # fmt: skip
    return res, job


def _no_kill_9_or_session(cmds):
    for c in cmds:
        assert (
            "-9" not in c
            and "KILL" not in c
            and "kill-session" not in c
            and "kill-server" not in c
        )


def test_stop_sends_ctrl_c_to_the_recorder_pane_only_and_reports_the_bag():
    sim = RobotSim(exit_after=1, alive_polls=2)
    res, job = _stop(sim)
    assert res == {
        "stopped": True, "robot": "hamilton", "pane": "%3", "output": "/home/swarm/run1/recorded_data",
        "size_bytes": 123456789, "metadata": True, "signals": ["SIGINT"],
    }  # fmt: skip
    keys = [c for c in sim.cmds if "send-keys" in c]
    assert keys == ["tmux send-keys -t %3 C-c"]  # never %4, never the session
    assert sim.cmds[0].startswith("echo '---REC---'")
    assert [c for c in sim.cmds if "kill -0" in c] == [recording.alive_command(104)] * 3
    assert (
        sim.cmds[-1]
        == "du -sb -- /home/swarm/run1/recorded_data 2>/dev/null | cut -f1; "
        "if [ -f /home/swarm/run1/recorded_data/metadata.yaml ]; then echo METADATA_OK; else echo METADATA_MISSING; fi"
    )
    assert not job.confirms
    assert any(
        lvl == "ok" and "123 MB" in t and "metadata.yaml written" in t
        for lvl, t in job.lines
    )
    _no_kill_9_or_session(sim.cmds)


def test_stop_timeout_offers_stop_harder_and_leaves_it_running_on_no():
    sim = RobotSim(exit_after=None, term_exits=False)
    res, job = _stop(sim, FakeJob(answer=False), wait_s=0.1)
    assert res is False
    assert len(job.confirms) == 1 and job.confirms[0][1]["yes_label"] == "Stop harder"
    assert "still running" in job.confirms[0][0]
    assert [c for c in sim.cmds if "send-keys" in c] == ["tmux send-keys -t %3 C-c"]
    assert not any(c.startswith("kill -TERM") for c in sim.cmds)
    _no_kill_9_or_session(sim.cmds)


def test_stop_harder_second_ctrl_c_then_sigterm():
    sim = RobotSim(exit_after=None, term_exits=True, metadata=False)
    res, job = _stop(sim, FakeJob(answer=True), wait_s=0.1)
    assert res["stopped"] and res["signals"] == ["SIGINT", "SIGINT", "SIGTERM"]
    assert res["metadata"] is False
    assert [c for c in sim.cmds if "send-keys" in c] == ["tmux send-keys -t %3 C-c"] * 2
    assert [c for c in sim.cmds if c.startswith("kill -TERM")] == ["kill -TERM 104"]
    assert any("ros2 bag reindex" in t for _, t in job.lines)
    _no_kill_9_or_session(sim.cmds)


def test_stop_harder_that_still_hangs_fails_without_kill_9():
    sim = RobotSim(exit_after=None, term_exits=False)
    res, job = _stop(sim, FakeJob(answer=True), wait_s=0.1)
    assert res is False
    assert any(lvl == "error" and "not killing it with -9" in t for lvl, t in job.lines)
    _no_kill_9_or_session(sim.cmds)


def test_stop_second_ctrl_c_alone_can_be_enough():
    sim = RobotSim(exit_after=2)
    res, _ = _stop(sim, FakeJob(answer=True), wait_s=0.1)
    assert res["signals"] == ["SIGINT", "SIGINT"]
    assert not any(c.startswith("kill -TERM") for c in sim.cmds)


def test_stop_when_nothing_records_or_wrong_pane():
    sim = RobotSim(exit_after=0)  # already gone: the probe finds nothing
    res, _ = _stop(sim)
    assert res == {"stopped": False, "was_recording": False}
    assert sim.cmds == [recording.probe_command()]
    sim = RobotSim()
    res, _ = _stop(sim, pane="%4")
    assert res == {"stopped": False, "was_recording": False}
    assert not any("send-keys" in c for c in sim.cmds)


def test_commands_reject_bad_input():
    for bad in ("", "3", "%3; reboot", "adt4_system", "%"):
        with pytest.raises(ValueError):
            recording.ctrl_c_command(bad)
    with pytest.raises(ValueError):
        recording.alive_command("1; reboot")
    assert recording.ctrl_c_command("%12") == "tmux send-keys -t %12 C-c"


# --------------------------------------------------------------- the route
def test_recording_stop_route_runs_a_job(settings):
    client, svc = make_client(settings)
    sim = RobotSim(exit_after=1)
    svc.helpers = sim
    robot = next(
        n for n, m in svc.fleet.runtime_config.items() if m.get("role") == "robot"
    )
    orig = recording.STOP_POLL_S
    recording.STOP_POLL_S = 0.01
    try:
        with client:
            r = client.post(f"/api/robots/{robot}/recording/stop", json={"pane": "%3"})
            assert r.status_code == 200, r.text
            job = svc.jobs.wait(r.json()["job_id"], timeout=10).to_dict()
            assert (
                job["kind"] == "recording_stop"
                and job["title"] == f"Stop bag on {robot}"
            )
            assert job["state"] == "succeeded", job["log"]
            assert job["result"]["metadata"] is True
            assert [c for c in sim.cmds if "send-keys" in c] == [
                "tmux send-keys -t %3 C-c"
            ]
            assert (
                client.post(
                    f"/api/robots/{robot}/recording/stop", json={"pane": "%3;x"}
                ).status_code
                == 400
            )
            assert client.post("/api/robots/nope/recording/stop").status_code == 404
    finally:
        recording.STOP_POLL_S = orig


def test_probe_module_runs_standalone():
    """The probe is stdlib only: it must run as ``python3 -`` with nothing else."""
    src = open(rp.__file__).read()
    r = subprocess.run([sys.executable, "-I", "-"], input=src, capture_output=True, text=True, timeout=30,
                       env={"PATH": os.environ.get("PATH", "")})  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert "recordings" in json.loads(r.stdout.strip().splitlines()[-1])
