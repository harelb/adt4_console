"""Launch: robots-only relaunch (the base station is left alone), and a robot
session always matches the robot's platform.

2026-11-11 (willow + euclid/hamilton + gauss): relaunching gauss alone sent it
the Spot session spot_relocalize-relocalize_awcd (launch memory held one legacy
``session`` and no per-platform ``sessions``), and relaunching a robot also
replaced the base station's session (planner restarted, panes lost)."""

import pytest

from adt4_console.launch_state import LaunchMemory
from adt4_console.routes import launch as launch_routes

from .conftest import make_client, needs_fleet_core
from .test_launch_topology import FakeSSH, _launch_deps


def _has_robots_only():
    try:
        from dcist_launch_system.fleet import launch_workflow as lw
    except ImportError:
        return False
    return hasattr(lw, "LaunchPlan") and "robots_only" in getattr(
        lw.LaunchPlan, "__dataclass_fields__", {}
    )


needs_robots_only = pytest.mark.skipif(
    not _has_robots_only(), reason="fleet tooling without robots-only launches"
)

# launch_memory.json when gauss was relaunched alone on 2026-11-11
NOV11_MEMORY = {
    "robots": ["gauss"],
    "session": "spot_relocalize-relocalize_awcd",
    "sessions": {},
    "bs_session": "base_station_web_grstaps_repair-grstaps_repair",
    "base_only": False,
    "experiment": "nov11_reloc_awcd",
    "mode": "relocalize",
    "prior_path": "/home/rrg/adt4_output/nov11_fused_awcd",
    "prior_source": "willow",
    "record": True,
    "base_station": "willow",
    "link_network": None,
    "link_mode": "fleetlink",
}


def _body(**kw):
    return {**NOV11_MEMORY, "preflight": False, **kw}


def _robot_line(lines, robot):
    return next(
        line for line in lines if line.startswith("ssh ") and f"-n {robot} " in line
    )


@needs_fleet_core
def test_nov11_launch_memory_runs_gauss_with_its_phoenix_session(client):
    lines = client.post("/api/launch/dry_run", json=_body()).json()["lines"]
    line = _robot_line(lines, "gauss")
    assert "run-adt4 phoenix_relocalize-relocalize_awcd -n gauss" in line
    assert "spot_" not in line
    # a Spot launched with the same memory keeps the Spot session
    lines = client.post(
        "/api/launch/dry_run", json=_body(robots=["euclid", "gauss"])
    ).json()["lines"]
    assert "run-adt4 spot_relocalize-relocalize_awcd -n euclid" in _robot_line(
        lines, "euclid"
    )
    assert "run-adt4 phoenix_relocalize-relocalize_awcd -n gauss" in _robot_line(
        lines, "gauss"
    )


@needs_fleet_core
def test_resolve_platform_sessions_maps_the_legacy_session():
    topo = {
        "machines": {
            "gauss": {"platform_type": "phoenix"},
            "euclid": {"platform_type": "spot"},
        }
    }
    rc = {
        "gauss": {"name": "gauss", "role": "robot"},
        "euclid": {"name": "euclid", "role": "robot"},
    }
    got = launch_routes.resolve_platform_sessions(
        {**NOV11_MEMORY, "robots": ["gauss", "euclid"]}, topo, rc
    )
    assert got == {
        "phoenix": "phoenix_relocalize-relocalize_awcd",
        "spot": "spot_relocalize-relocalize_awcd",
    }
    # an explicit per-platform pick wins
    got = launch_routes.resolve_platform_sessions(
        {**NOV11_MEMORY, "sessions": {"phoenix": "phoenix_relocalize-relocalize"}},
        topo,
        rc,
    )
    assert got == {"phoenix": "phoenix_relocalize-relocalize"}
    assert launch_routes.session_platform("phoenix-default") == "phoenix"
    assert launch_routes.session_platform("base_station_web-default") is None


@needs_fleet_core
def test_session_of_another_platform_is_refused(client):
    body = _body(sessions={"phoenix": "spot_relocalize-relocalize_awcd"})
    for url in ("/api/launch", "/api/launch/dry_run"):
        r = client.post(url, json=body)
        assert r.status_code == 400, url
        d = r.json()["detail"]
        assert (
            "gauss (platform phoenix)" in d and "spot_relocalize-relocalize_awcd" in d
        )


@needs_fleet_core
@needs_robots_only
def test_robots_only_dry_run_has_no_base_command(client):
    lines = client.post("/api/launch/dry_run", json=_body(robots_only=True)).json()[
        "lines"
    ]
    ssh = [line for line in lines if line.startswith("ssh ")]
    assert len(ssh) == 1 and "-n gauss " in ssh[0] and "nohup" not in ssh[0]
    assert any("Robots only" in line for line in lines)
    r = client.post("/api/launch/dry_run", json=_body(robots_only=True, robots=[]))
    assert r.status_code == 400 and "Robots only" in r.json()["detail"]
    r = client.post("/api/launch/dry_run", json=_body(robots_only=True, base_only=True))
    assert r.status_code == 400


@needs_fleet_core
@needs_robots_only
def test_robots_only_relaunch_leaves_the_base_alone(settings, tmp_path):
    settings.launch_memory_path = str(tmp_path / "launch_memory.json")
    gauss_ip, willow_ip = "198.51.100.13", "198.51.100.4"
    calls = []
    ssh = FakeSSH([
        ("show-environment", (0, "ADT4_OUTPUT_DIR=/home/swarm/out/nov11_reloc_awcd", "")),
        ("pane_current_command", (0, "zsh\n", "")),
        ("_prev-", (0, "MOVED /home/swarm/out/nov11_reloc_awcd_prev-20261111-2215\n", "")),
        ("echo OK", (0, "OK", "")),
    ])  # fmt: skip

    def ssh_ip(user, ip, cmd, timeout=5):
        calls.append((ip, cmd))
        return ssh(user, ip, cmd, timeout)

    deps = _launch_deps(ssh_ip)
    deps.deploy_zenoh = lambda *a: (_ for _ in ()).throw(
        AssertionError("zenoh deployed")
    )
    deps.rsync = lambda *a, **k: (True, "direct", "ok")
    deps.local_ips = lambda: set()
    client, svc = make_client(settings, launch_deps=deps)
    with client:
        # the full launch of the night is remembered
        svc.launch_memory.remember({**NOV11_MEMORY, "robots": ["euclid", "hamilton", "gauss"],
                                    "sessions": {"spot": "spot_relocalize-relocalize_awcd"}})  # fmt: skip
        svc.launch_state.set({"experiment": "nov11_reloc_awcd", "robots": ["euclid", "hamilton"],
                              "mode": "relocalize", "base_station": "willow", "prior": {"run_id": "r1"}})  # fmt: skip
        r = client.post("/api/launch", json=_body(robots_only=True))
        assert r.status_code == 200, r.json()
        job = svc.jobs.wait(r.json()["job_id"]).to_dict()
        text = [entry["text"] for entry in job["log"]]
        assert job["state"] == "succeeded", text
        assert "robots only" in job["title"]
        assert job.get("confirm") is None  # the restart needs no second question
        willow = [c for ip, c in calls if ip == willow_ip]
        assert not any(
            k in c
            for c in willow
            for k in ("kill-session", "send-keys", "nohup", "run-adt4", "rm -rf")
        )
        gauss = [c for ip, c in calls if ip == gauss_ip]
        assert any("send-keys" in c and "C-c" in c for c in gauss)
        assert not any("rm -rf" in c for c in gauss)
        [run] = [c for c in gauss if "run-adt4" in c]
        assert (
            "run-adt4 phoenix_relocalize-relocalize_awcd -n gauss" in run
            and " -r " in run
        )
        assert not any(ip not in (gauss_ip, willow_ip) for ip, _ in calls)
        # the full launch's robots, base session and prior stay remembered / active
        last = client.get("/api/launch/last", params={"base_station": "willow"}).json()[
            "last"
        ]
        assert last["robots"] == ["euclid", "hamilton", "gauss"]
        assert last["bs_session"] == "base_station_web_grstaps_repair-grstaps_repair"
        assert last["sessions"]["phoenix"] == "phoenix_relocalize-relocalize_awcd"
        assert last["sessions"]["spot"] == "spot_relocalize-relocalize_awcd"
        active = svc.launch_state.get()
        assert active["robots"] == ["euclid", "hamilton", "gauss"] and active[
            "prior"
        ] == {"run_id": "r1"}


@needs_fleet_core
@needs_robots_only
def test_robots_only_save_first_aborts_while_hydra_saves(
    settings, tmp_path, monkeypatch
):
    settings.launch_memory_path = str(tmp_path / "launch_memory.json")
    ssh = FakeSSH([("echo OK", (0, "OK", ""))])
    client, svc = make_client(settings, launch_deps=_launch_deps(ssh))
    saved = []
    monkeypatch.setattr(
        launch_routes, "stop_save_run",
        lambda svc, job, robot, teardown=True: saved.append(robot) or {"method": "tmux", "teardown": False},
    )  # fmt: skip
    with client:
        r = client.post("/api/launch", json=_body(robots_only=True, save_first=True))
        job = svc.jobs.wait(r.json()["job_id"]).to_dict()
        assert saved == ["gauss"]
        assert job["state"] == "failed"
        assert any("still saving" in entry["text"] for entry in job["log"])
        assert not ssh.ran("run-adt4")


def test_memory_robots_only_keeps_the_full_launch(tmp_path):
    mem = LaunchMemory(str(tmp_path / "m.json"))
    mem.remember(
        {
            **NOV11_MEMORY,
            "robots": ["euclid", "gauss"],
            "sessions": {"spot": "spot_relocalize-relocalize"},
        }
    )

    class P:
        robots = ["gauss"]
        sessions = {"phoenix": "phoenix_relocalize-relocalize_awcd"}

    body = launch_routes.LaunchPlanBody(**_body(robots_only=True, experiment="other"))
    e = launch_routes.remember_launch(mem, body, P)
    assert e["robots"] == ["euclid", "gauss"] and e["experiment"] == "nov11_reloc_awcd"
    assert e["sessions"] == {
        "spot": "spot_relocalize-relocalize",
        "phoenix": "phoenix_relocalize-relocalize_awcd",
    }
    assert e["base_only"] is False and e["bs_session"] == NOV11_MEMORY["bs_session"]
