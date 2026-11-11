"""The Launch form's last choices (prior map, sessions, ...) survive relaunches."""

import json

from adt4_console.launch_state import LaunchMemory, memory_from_active

from .conftest import make_client, needs_fleet_core
from .test_launch_topology import FakeSSH, _launch_deps


def _plan(**kw):
    return {
        "robots": ["hamilton"],
        "session": "spot-reloc",
        "experiment": "courtyard_reloc",
        "mode": "relocalize",
        "prior_path": "/home/swarm/adt4_output/courtyard_fused",
        "prior_source": "sequoia",
        "record": False,
        "base_station": "sequoia",
        "bs_session": "base_station_fleetlink",
        "link_network": None,
        "link_mode": "fleetlink",
        "preflight": False,
        **kw,
    }


def test_memory_persists_per_base_station(tmp_path):
    path = tmp_path / "state" / "launch_memory.json"
    mem = LaunchMemory(str(path))
    assert mem.get() is None and mem.get("sequoia") is None
    mem.remember(_plan())
    mem.remember(_plan(base_station="willow", prior_path="/out/other", experiment="w1"))
    # a console restart reads the file back
    again = LaunchMemory(str(path))
    s = again.get("sequoia")
    assert s["prior_path"] == "/home/swarm/adt4_output/courtyard_fused"
    assert (
        s["prior_source"] == "sequoia" and s["bs_session"] == "base_station_fleetlink"
    )
    assert s["mode"] == "relocalize" and s["link_mode"] == "fleetlink"
    assert "preflight" not in s and s["saved_at"] > 0
    assert again.get("willow")["prior_path"] == "/out/other"
    # unknown base / no base: the most recent launch
    assert again.get("euclid")["experiment"] == "w1"
    assert again.get()["experiment"] == "w1"
    # launches without a base station are remembered too
    again.remember(_plan(base_station=None, experiment="robots_only"))
    assert again.get()["experiment"] == "robots_only"
    assert json.loads(path.read_text())["last"] == "-"


def test_memory_without_a_path_and_with_a_bad_file(tmp_path):
    mem = LaunchMemory(None)
    mem.remember(_plan())
    assert mem.get("sequoia")["experiment"] == "courtyard_reloc"
    bad = tmp_path / "m.json"
    bad.write_text("{not json")
    assert LaunchMemory(str(bad)).get() is None


def test_memory_from_active_launch_record():
    assert memory_from_active(None) is None
    m = memory_from_active(
        {
            "experiment": "e1",
            "robots": ["gauss"],
            "mode": "relocalize",
            "base_station": "sequoia",
            "started_at": 5.0,
            "prior": {"path": "/out/p", "source_machine": "hamilton"},
        }
    )
    assert m["prior_path"] == "/out/p" and m["prior_source"] == "hamilton"
    assert m["robots"] == ["gauss"] and m["mode"] == "relocalize"


def test_defaults_fall_back_to_the_active_launch(settings):
    client, svc = make_client(settings)
    with client:
        r = client.get("/api/launch/last").json()
        assert r["last"] is None
        svc.launch_state.set(
            {
                "experiment": "e1",
                "robots": ["euclid"],
                "mode": "mapping",
                "base_station": None,
                "started_at": 1.0,
                "prior": {"path": "/out/p", "source_machine": "sequoia"},
            }
        )
        assert client.get("/api/launch/last").json()["last"]["prior_path"] == "/out/p"


@needs_fleet_core
def test_launch_remembers_the_form_and_defaults_return_it(settings, tmp_path):
    settings.launch_memory_path = str(tmp_path / "launch_memory.json")
    ssh = FakeSSH([("tmux has-session", (1, "", "")), ("test -d", (1, "", ""))])
    client, svc = make_client(settings, launch_deps=_launch_deps(ssh))
    plan = _plan(robots=["euclid"], session="spot-default", base_station=None,
                 bs_session=None, link_mode=None, prior_path="", prior_source=None,
                 mode="mapping")  # fmt: skip
    with client:
        d = client.get("/api/launch/defaults", params={"mode": "mapping"}).json()
        assert d["last"] is None
        job_id = client.post("/api/launch", json=plan).json()["job_id"]
        svc.jobs.wait(job_id)
        d = client.get("/api/launch/defaults", params={"mode": "mapping"}).json()
        assert d["last"]["robots"] == ["euclid"]
        assert d["last"]["experiment"] == "courtyard_reloc"
        assert d["last"]["session"] == "spot-default"
        # with a prior: remembered even before the launch job finishes
        plan2 = {**plan, "prior_path": "/home/swarm/adt4_output/courtyard_fused",
                 "prior_source": "euclid", "experiment": "courtyard_reloc_v2"}  # fmt: skip
        svc.jobs.wait(client.post("/api/launch", json=plan2).json()["job_id"])
        last = client.get("/api/launch/defaults").json()["last"]
        assert last["prior_path"] == "/home/swarm/adt4_output/courtyard_fused"
        assert last["prior_source"] == "euclid"
        assert last["experiment"] == "courtyard_reloc_v2"
        # a refused plan is not remembered
        r = client.post("/api/launch", json={**plan, "experiment": ""})
        assert r.status_code == 400
        assert (
            client.get("/api/launch/last").json()["last"]["experiment"]
            == "courtyard_reloc_v2"
        )
    # survives a console restart
    assert LaunchMemory(settings.launch_memory_path).get()["prior_source"] == "euclid"
