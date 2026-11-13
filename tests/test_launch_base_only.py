"""Launch: base station only, and one robot session per platform."""

import pytest

from adt4_console.launch_state import LaunchMemory

from .conftest import make_client, needs_fleet_core
from .test_launch_topology import FakeSSH, _launch_deps


def _has_base_only():
    try:
        from dcist_launch_system.fleet import launch_workflow as lw
    except ImportError:
        return False
    return hasattr(lw, "validate_plan")


# fleet tooling with base-only launches / per-platform sessions
needs_base_only = pytest.mark.skipif(
    not _has_base_only(), reason="fleet tooling without base-only launches"
)

BASE_ONLY = {
    "robots": [],
    "experiment": "exp1",
    "base_only": True,
    "base_station": "willow",
    "bs_session": "base_station_web-default",
    "preflight": False,
}


@needs_fleet_core
@needs_base_only
def test_base_only_dry_run_shows_only_the_base_command(client):
    lines = client.post("/api/launch/dry_run", json=BASE_ONLY).json()["lines"]
    ssh = [line for line in lines if line.startswith("ssh ")]
    assert len(ssh) == 1 and "run-adt4 base_station_web-default -n willow" in ssh[0]
    # robots in the body are ignored in base-only mode
    lines2 = client.post(
        "/api/launch/dry_run", json={**BASE_ONLY, "robots": ["euclid"]}
    ).json()["lines"]
    assert lines2 == lines
    r = client.post("/api/launch/dry_run", json={**BASE_ONLY, "bs_session": None})
    assert r.status_code == 400 and "base station" in r.json()["detail"].lower()


@needs_fleet_core
@needs_base_only
def test_base_only_launch_restarts_only_the_base(settings, tmp_path):
    settings.launch_memory_path = str(tmp_path / "launch_memory.json")
    ssh = FakeSSH(
        [
            ("echo OK", (0, "OK", "")),
            ("tmux has-session", (1, "", "")),
            ("test -d", (1, "", "")),
        ]
    )
    deps = _launch_deps(ssh)
    deps.deploy_zenoh = lambda user, ip, eps: (True, "deployed")
    deps.local_ips = lambda: set()
    client, svc = make_client(settings, launch_deps=deps)
    with client:
        # an earlier robots + base launch is remembered
        full = {**BASE_ONLY, "base_only": False, "robots": ["euclid", "gauss"],
                "sessions": {"spot": "spot-default", "phoenix": "phoenix-default"}}  # fmt: skip
        svc.jobs.wait(client.post("/api/launch", json=full).json()["job_id"])
        ssh.calls.clear()
        r = client.post("/api/launch", json={**BASE_ONLY, "robots": ["euclid"]})
        job = svc.jobs.wait(r.json()["job_id"]).to_dict()
        assert job["state"] == "succeeded", [e["text"] for e in job["log"]]
        assert "base station willow only" in job["title"]
        launched = ssh.ran("run-adt4")
        assert (
            len(launched) == 1 and "base_station_web-default -n willow" in launched[0]
        )
        last = client.get("/api/launch/last", params={"base_station": "willow"}).json()[
            "last"
        ]
        assert last["base_only"] is True
        # robots + sessions of the previous launch are kept for the next full launch
        assert last["robots"] == ["euclid", "gauss"]
        assert last["sessions"] == {
            "spot": "spot-default",
            "phoenix": "phoenix-default",
        }
        # the active record keeps the running robots
        assert svc.launch_state.get()["robots"] == ["euclid", "gauss"]


@needs_fleet_core
@needs_base_only
def test_per_platform_sessions(client):
    plan = {"robots": ["euclid", "gauss"], "experiment": "e", "preflight": False,
            "sessions": {"spot": "spot_relocalize-relocalize", "phoenix": "phoenix_relocalize-relocalize"},
            "mode": "relocalize", "prior_path": "/out/p"}  # fmt: skip
    lines = client.post("/api/launch/dry_run", json=plan).json()["lines"]
    by = {
        n: next(line for line in lines if f"-n {n} " in line)
        for n in ("euclid", "gauss")
    }
    assert "run-adt4 spot_relocalize-relocalize" in by["euclid"]
    assert "run-adt4 phoenix_relocalize-relocalize" in by["gauss"]
    # a platform without a session is refused
    r = client.post(
        "/api/launch", json={**plan, "sessions": {"spot": "spot_relocalize-relocalize"}}
    )
    assert r.status_code == 400 and "phoenix" in r.json()["detail"]
    d = client.get("/api/launch/defaults", params={"mode": "mapping"}).json()
    assert (
        d["robot_platforms"]["gauss"] == "phoenix"
        and d["robot_platforms"]["euclid"] == "spot"
    )
    assert d["platform_sessions"] == {
        "spot": "spot-default",
        "phoenix": "phoenix-default",
    }


def test_memory_base_only_without_a_previous_launch(tmp_path):
    mem = LaunchMemory(str(tmp_path / "m.json"))
    e = mem.remember({**BASE_ONLY, "robots": ["euclid"]})
    assert e["base_only"] is True and e["robots"] is None
