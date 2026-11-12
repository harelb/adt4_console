"""Per-run output dirs (run-adt4: ``<exp>/runs/<run_id>`` + ``<exp>/latest``) in the
console: maps load through ``latest``, transfers keep the layout, robots get no image
store, index builds the latest run's store."""

import os
import pathlib
import shlex

import pytest

from adt4_console import run_layout
from adt4_console.graph_service import FileSource

from .test_maps_listing import make_store


def make_exp_run(exp: pathlib.Path, run_id: str, dsg=True, latest=True) -> pathlib.Path:
    """``<exp>/runs/<run_id>`` with a map (and ``latest`` pointing at it)."""
    run = exp / "runs" / run_id
    (run / "roman").mkdir(parents=True)
    (run / "roman" / "1.roman_map.pkl").write_bytes(b"r" * 100)
    if dsg:
        (run / "hydra" / "backend").mkdir(parents=True)
        (run / "hydra" / "backend" / "dsg.json").write_text('{"nodes": []}')
    if latest:
        link = exp / "latest"
        if link.is_symlink():
            link.unlink()
        link.symlink_to(os.path.join("runs", run_id))
    return run


# --- run_layout ------------------------------------------------------------------


def test_flat_dir_is_its_own_run(tmp_path):
    flat = tmp_path / "old_map"
    (flat / "hydra" / "backend").mkdir(parents=True)
    (flat / "hydra" / "backend" / "dsg.json").write_text("{}")
    assert run_layout.resolve_run_dir(flat) == flat
    assert run_layout.find_dsg(flat) == (flat / "hydra/backend/dsg.json", flat)
    assert run_layout.display_name(flat) == "old_map"


def test_experiment_resolves_latest_else_newest_run_with_a_map(tmp_path):
    exp = tmp_path / "exp"
    first = make_exp_run(exp, "willow_20261111-100000_aaaa")
    assert run_layout.resolve_run_dir(exp) == first.resolve()
    dsg, run = run_layout.find_dsg(exp)
    assert run == first.resolve() and dsg == first.resolve() / "hydra/backend/dsg.json"
    assert run_layout.display_name(run) == "exp"
    # a second launch crashed before saving: latest has no map, the first run's is used
    second = make_exp_run(exp, "willow_20261111-110000_bbbb", dsg=False)
    assert run_layout.resolve_run_dir(exp) == second.resolve()
    assert run_layout.find_dsg(exp)[1] == first
    assert run_layout.find_dsg(tmp_path / "nothing") is None


def test_run_subpath_of_scan_entries():
    e = {"abs_path": "/h/adt4_output/exp", "run_dir": "/h/adt4_output/exp/runs/r1"}
    assert run_layout.run_subpath(e) == "runs/r1"
    assert run_layout.entry_run_dir(e) == "/h/adt4_output/exp/runs/r1"
    # a symlinked output root: run_dir is a realpath
    e = {"abs_path": "/home/x/adt4_output/exp", "run_dir": "/data/out/exp/runs/r2"}
    assert run_layout.run_subpath(e) == "runs/r2"
    flat = {"abs_path": "/h/adt4_output/old", "run_dir": None}
    assert (
        run_layout.run_subpath(flat) == ""
        and run_layout.entry_run_dir(flat) == "/h/adt4_output/old"
    )


def test_file_source_on_an_experiment_dir_reads_latest(tmp_path):
    exp = tmp_path / "nov_11_test"
    run = make_exp_run(exp, "hamilton_20261111-185801_a3f2")
    src = FileSource("prior", exp)
    assert src.dsg_path == run.resolve() / "hydra/backend/dsg.json"
    assert src.run_dir == run.resolve() and src.exp_dir == exp
    assert src.label == "prior (nov_11_test)"


# --- transfers / index (fleet core) ------------------------------------------------

fleet_core = pytest.importorskip(
    "dcist_launch_system.fleet", reason="needs the fleet core"
)
from adt4_console.routes import maps as maps_route  # noqa: E402

from .conftest import make_client  # noqa: E402
from .test_index_images import FakeIndexer, fresh_envs  # noqa: E402,F401
from .test_run_identity_maps import (  # noqa: E402,F401
    HAMILTON_IP,
    _wait,
    fleet,
    fresh_cache,
)


def _exp_on(root: pathlib.Path, name="map_x") -> pathlib.Path:
    from dcist_launch_system.fleet import run_identity as ri

    exp = root / name
    run = make_exp_run(exp, "willow_20261111-100000_aaaa")
    ri.write_run_info(run, robot="willow", platform="base_station", session="base_station-default",
                      run_id="willow_20261111-100000_aaaa")  # fmt: skip
    make_store(run, keyframes=3)
    (run / "mission_bag").mkdir()
    (run / "mission_bag" / "mission_bag_0.mcap").write_bytes(b"m" * 10)
    (run / "ros_log").mkdir()
    (run / "ros_log" / "x.log").write_text("log")
    return exp


def test_transfer_to_a_robot_keeps_layout_without_store_bags_or_logs(settings, fleet):
    _exp_on(fleet.root())
    c, svc = make_client(settings, helpers=fleet.helpers())
    with c:
        body = {"src": "local", "dst": "hamilton", "experiment": "map_x"}
        job = _wait(svc, c.post("/api/maps/transfer", json=body).json()["job_id"])
        assert job["state"] == "succeeded", job["log"]
    copy = fleet.root(HAMILTON_IP) / job["result"]["dest_name"]
    run = copy / "runs" / "willow_20261111-100000_aaaa"
    assert (copy / "latest").is_symlink() and (
        copy / "latest" / "hydra/backend/dsg.json"
    ).is_file()
    assert (run / "run_info.json").is_file() and (run / "roman").is_dir()
    for gone in ("image_store", "mission_bag", "ros_log"):
        assert not (run / gone).exists(), gone
    assert "image_store" not in job["result"]
    assert any("is a robot" in line["text"] for line in job["log"])


def test_transfer_to_the_base_keeps_the_store_but_not_bags_or_logs(settings, fleet):
    _exp_on(fleet.root(HAMILTON_IP))
    c, svc = make_client(settings, helpers=fleet.helpers())
    with c:
        body = {"src": "hamilton", "experiment": "map_x"}
        job = _wait(svc, c.post("/api/maps/transfer", json=body).json()["job_id"])
        assert job["state"] == "succeeded", job["log"]
    run = (
        fleet.root()
        / job["result"]["dest_name"]
        / "runs"
        / "willow_20261111-100000_aaaa"
    )
    assert (run / "image_store" / "store.sqlite").is_file()
    assert not (run / "mission_bag").exists() and not (run / "ros_log").exists()


def test_packed_transfer_pipes_the_latest_runs_store(settings, fleet, monkeypatch):
    exp = _exp_on(fleet.root(HAMILTON_IP))
    piped = []

    def fake_pipe(job, svc, src, dst, src_run, dst_run, images, compression):
        piped.append((src_run, dst_run))
        return True, "ok"

    monkeypatch.setattr(maps_route, "pipe_store", fake_pipe)
    c, svc = make_client(settings, helpers=fleet.helpers())
    with c:
        body = {
            "src": "hamilton",
            "experiment": "map_x",
            "images": "keyframes",
            "compression": "fast",
        }
        job = _wait(svc, c.post("/api/maps/transfer", json=body).json()["job_id"])
        assert job["state"] == "succeeded", job["log"]
    [(src_run, dst_run)] = piped
    assert (
        pathlib.Path(src_run).resolve()
        == (exp / "runs" / "willow_20261111-100000_aaaa").resolve()
    )
    assert dst_run.endswith(
        f"/{job['result']['dest_name']}/runs/willow_20261111-100000_aaaa"
    )
    # the rsync left the store and raw logs of the run for the pack
    copy_run = (
        fleet.root()
        / job["result"]["dest_name"]
        / "runs"
        / "willow_20261111-100000_aaaa"
    )
    assert (copy_run / "hydra/backend/dsg.json").is_file() and not (
        copy_run / "image_store"
    ).exists()


def test_index_images_builds_the_latest_runs_store(settings, fleet, monkeypatch):
    exp = fleet.root(HAMILTON_IP) / "map_x"
    run = make_exp_run(exp, "hamilton_20261111-100000_cccc")
    (run / "keyframes").mkdir()
    (run / "keyframes" / "1000.zip").write_bytes(b"PK" * 100)
    fake = FakeIndexer(run)
    monkeypatch.setattr(maps_route, "stream_command", fake)
    c, svc = make_client(settings, helpers=fleet.helpers())
    with c:
        r = c.post(
            "/api/maps/index_images", json={"machine": "hamilton", "name": "map_x"}
        )
        assert r.status_code == 200, r.text
        job = _wait(svc, r.json()["job_id"])
        assert job["state"] == "succeeded", job["log"]
    (index_cmd, _), (info_cmd, _) = fake.calls
    script = shlex.split(index_cmd[-1])[2]
    assert script.endswith(f"index {run.resolve()}")
    assert shlex.split(info_cmd[-1])[2].endswith(f"info {run.resolve()}/image_store")
