"""Maps → Build image store: POST /api/maps/index_images, and the remote python
resolution (ADT4_ENV / ADT4_WS from the machine's ~/.zshrc) shared with transfers."""

import shlex
import threading

import pytest

pytest.importorskip("dcist_launch_system.fleet", reason="needs the fleet core")
from adt4_console.routes import maps as maps_route  # noqa: E402

from .conftest import make_client  # noqa: E402
from .test_maps_listing import make_store  # noqa: E402
from .test_run_identity_maps import (  # noqa: E402,F401  # noqa: E402,F401
    HAMILTON_IP,
    _wait,
    fleet,
    fresh_cache,
    make_run,
)

HAMILTON_ENV = (
    "\n__ADT4_ENV=/home/swarm/environments/dcist\n__ADT4_WS=/home/swarm/dcist_ws\n"
)


@pytest.fixture(autouse=True)
def fresh_envs(monkeypatch):
    monkeypatch.setattr(maps_route, "_remote_envs", {})
    monkeypatch.setattr(maps_route, "_indexing", {})
    monkeypatch.delenv("ADT4_ENV", raising=False)
    monkeypatch.delenv("ADT4_WS", raising=False)


def with_env_probe(fleet, out=HAMILTON_ENV, rc=0):
    """fleet helpers whose ssh_cmd answers ENV_PROBE with ``out``; probes counted."""
    h = fleet.helpers()
    real, probes = h.ssh_cmd, []

    def ssh_cmd(user, ip, cmd, timeout=5):
        if cmd == maps_route.ENV_PROBE:
            probes.append(ip)
            return rc, out, ""
        return real(user, ip, cmd, timeout)

    h.ssh_cmd = ssh_cmd
    return h, probes


def raw_run(root, name="map_x"):
    run = make_run(root, name, robot="hamilton")
    (run / "keyframes").mkdir()
    (run / "keyframes" / "1000.zip").write_bytes(b"PK" * 100)
    return run


class FakeIndexer:
    """stream_command stand-in: records argv; ``index`` writes a store."""

    def __init__(self, run=None, rc=0, out=""):
        self.calls, self.run, self.rc, self.out = [], run, rc, out
        self.release = threading.Event()
        self.release.set()

    def __call__(self, job, cmd, env, keep=30):
        self.calls.append((cmd, env))
        self.release.wait(5)
        text = cmd[-1] if env is None else " ".join(cmd)
        if " index " in f" {text} " and self.rc == 0 and self.run is not None:
            make_store(self.run, keyframes=5)
        if self.out:
            job.log("dim", self.out)
        return self.rc, self.out


# --- ADT4_ENV probe -------------------------------------------------------------


def test_parse_remote_env_ignores_noise():
    out = (
        "Welcome to Ubuntu\x1b[0m\nlast login: x __ADT4_ENVIRONMENT\n"
        "oh-my-zsh banner__ADT4_ENV=/home/swarm/environments\r\n"
        "__ADT4_WS=/home/swarm/dcist_ws/\nmore noise __ADT4_ENV=/other\n"
    )
    assert maps_route.parse_remote_env(out) == {
        "ADT4_ENV": "/home/swarm/environments",
        "ADT4_WS": "/home/swarm/dcist_ws",
    }


@pytest.mark.parametrize(
    "out",
    ["", "noise only\n", "\n__ADT4_ENV=\n__ADT4_WS=\n", "__ADT4_ENV=relative/x\n"],
)
def test_parse_remote_env_empty_or_bad_values_are_left_out(out):
    assert maps_route.parse_remote_env(out) == {}


def test_env_probe_reads_interactive_zsh_with_markers():
    assert "zsh -ic" in maps_route.ENV_PROBE
    assert (
        "__ADT4_ENV=%s" in maps_route.ENV_PROBE
        and "__ADT4_WS=%s" in maps_route.ENV_PROBE
    )
    assert "</dev/null" in maps_route.ENV_PROBE


def _svc(helpers, local_ips=("127.0.0.1",)):
    import types

    helpers.get_local_ips = lambda: list(local_ips)
    return types.SimpleNamespace(helpers=helpers)


def test_store_command_exports_remote_env_and_caches_it(fleet):
    h, probes = with_env_probe(fleet)
    svc = _svc(h)
    m = {"name": "hamilton", "user": "swarm", "ip": HAMILTON_IP}
    argv, env = maps_route.store_command(svc, m, ["pack", "~/adt4_output/map_x", "-"])
    assert env is None and argv[:1] == ["ssh"] and argv[-2] == f"swarm@{HAMILTON_IP}"
    script = shlex.split(argv[-1])[2]  # sh -c <script>
    assert script.startswith(
        "ADT4_ENV=/home/swarm/environments/dcist; export ADT4_ENV; "
        "ADT4_WS=/home/swarm/dcist_ws; export ADT4_WS; "
    )
    assert maps_route.REMOTE_PY in script and "import fleetlink.image_pack" in script
    assert script.endswith('exec "$PY" -m "$MOD" pack $HOME/adt4_output/map_x -')
    maps_route.store_command(svc, m, ["unpack", "-", "x"])
    assert probes == [HAMILTON_IP]  # once per machine


def test_store_command_without_zshrc_values_falls_back(fleet):
    h, probes = with_env_probe(fleet, out="banner\n__ADT4_ENV=\n__ADT4_WS=\n")
    m = {"name": "euclid", "user": "swarm", "ip": "10.0.0.9"}
    argv, _ = maps_route.store_command(
        _svc(h), m, ["index", "/r"], maps_route.INDEX_MODULE
    )
    script = shlex.split(argv[-1])[2]
    assert "ADT4_ENV=" not in script.split("PY=")[0]  # nothing exported
    assert "${ADT4_ENV:-$HOME/environments/dcist}/spark_env/bin/python3" in script
    assert (
        "MOD=hydra_python.image_store; " in script and "import fleetlink" not in script
    )


def test_failed_probe_is_not_cached(fleet):
    h, probes = with_env_probe(fleet, out="", rc=255)
    m = {"name": "euclid", "user": "swarm", "ip": "10.0.0.9"}
    assert maps_route.remote_env(_svc(h), m) == {}
    assert maps_route.remote_env(_svc(h), m) == {}
    assert len(probes) == 2


def test_remote_script_runs_and_names_its_python(tmp_path, monkeypatch):
    """The generated script itself, run by sh: picks $ADT4_ENV/spark_env/bin/python3
    and says so on stderr."""
    import subprocess

    env_dir = tmp_path / "venvs"
    py = env_dir / "spark_env" / "bin" / "python3"
    py.parent.mkdir(parents=True)
    py.write_text('#!/bin/sh\necho "ran $@"\n')
    py.chmod(0o755)
    import types

    h = types.SimpleNamespace(
        ssh_cmd=lambda *a, **k: (0, f"__ADT4_ENV={env_dir}\n", ""),
        get_local_ips=lambda: ["127.0.0.1"],
    )
    m = {"name": "euclid", "user": "swarm", "ip": "10.0.0.9"}
    argv, _ = maps_route.store_command(
        _svc(h), m, ["index", "/r"], maps_route.INDEX_MODULE
    )
    r = subprocess.run(
        ["sh", "-c", shlex.split(argv[-1])[2]], capture_output=True, text=True
    )
    assert r.stdout.strip() == "ran -m hydra_python.image_store index /r"
    assert f"python: {py} -m hydra_python.image_store" in r.stderr


# --- POST /api/maps/index_images ------------------------------------------------


def test_index_remote_run_builds_store_and_refreshes_listing(
    settings, fleet, monkeypatch
):
    run = raw_run(fleet.root(HAMILTON_IP))
    fake = FakeIndexer(run)
    monkeypatch.setattr(maps_route, "stream_command", fake)
    h, _ = with_env_probe(fleet)
    c, svc = make_client(settings, helpers=h)
    with c:
        before = c.get("/api/maps").json()["machines"]["hamilton"]
        (row,) = [e for e in before if e["name"] == "map_x"]
        assert row["image_store"]["indexed"] is False
        r = c.post(
            "/api/maps/index_images", json={"machine": "hamilton", "name": "map_x"}
        )
        assert r.status_code == 200, r.text
        job = _wait(svc, r.json()["job_id"])
        assert job["state"] == "succeeded", job["log"]
        assert job["kind"] == "index_images"
        (index_cmd, env), (info_cmd, _) = fake.calls
        assert env is None and index_cmd[0] == "ssh"
        script = shlex.split(index_cmd[-1])[2]
        assert "ADT4_ENV=/home/swarm/environments/dcist; export ADT4_ENV;" in script
        assert script.endswith(f'exec "$PY" -m "$MOD" index {run}')
        assert "MOD=hydra_python.image_store;" in script
        assert shlex.split(info_cmd[-1])[2].endswith(f"info {run}/image_store")
        assert job["result"]["image_store"]["frames"]["keyframe"] == 5
        assert any(
            "ADT4_ENV=/home/swarm/environments/dcist" in line["text"]
            for line in job["log"]
        )
        after = c.get("/api/maps").json()["machines"]["hamilton"]
        (row,) = [e for e in after if e["name"] == "map_x"]
        assert row["image_store"]["frames"]["keyframe"] == 5


def test_index_local_run_uses_this_python(settings, fleet, monkeypatch):
    import sys

    run = raw_run(fleet.root(), "base_run")
    fake = FakeIndexer(run)
    monkeypatch.setattr(maps_route, "stream_command", fake)
    c, svc = make_client(settings, helpers=fleet.helpers())
    with c:
        r = c.post(
            "/api/maps/index_images",
            json={"machine": "local", "name": "base_run", "partial": True},
        )
        job = _wait(svc, r.json()["job_id"])
        assert job["state"] == "succeeded", job["log"]
        (cmd, env), (info, _) = fake.calls
        assert cmd == [
            sys.executable,
            "-m",
            "hydra_python.image_store",
            "index",
            str(run),
            "--partial",
        ]
        assert env is not None
        assert info == [
            sys.executable,
            "-m",
            "hydra_python.image_store",
            "info",
            f"{run}/image_store",
        ]


def test_index_failure_reports_output(settings, fleet, monkeypatch):
    raw_run(fleet.root(HAMILTON_IP))
    fake = FakeIndexer(rc=1, out="ModuleNotFoundError: No module named 'hydra_python'")
    monkeypatch.setattr(maps_route, "stream_command", fake)
    h, _ = with_env_probe(fleet)
    c, svc = make_client(settings, helpers=h)
    with c:
        r = c.post(
            "/api/maps/index_images", json={"machine": "hamilton", "name": "map_x"}
        )
        job = _wait(svc, r.json()["job_id"])
        assert job["state"] == "failed"
        assert len(fake.calls) == 1  # no info after a failed index
        assert any(
            line["level"] == "error"
            and "rc 1" in line["text"]
            and "hydra_python" in line["text"]
            for line in job["log"]
        )
        # the guard is released: a retry starts
        r = c.post(
            "/api/maps/index_images", json={"machine": "hamilton", "name": "map_x"}
        )
        assert r.status_code == 200


def test_index_rejects_bad_input(settings, fleet, monkeypatch):
    make_run(fleet.root(HAMILTON_IP), "no_images", robot="hamilton")
    monkeypatch.setattr(maps_route, "stream_command", FakeIndexer())
    h, _ = with_env_probe(fleet)
    c, svc = make_client(settings, helpers=h)
    with c:
        post = lambda **b: c.post("/api/maps/index_images", json=b)  # noqa: E731
        for bad in ("../etc", "a/b", "..", ".hidden", "-rf", "x\ny", ""):
            assert post(machine="hamilton", name=bad).status_code == 400, bad
        assert post(machine="nosuch", name="map_x").status_code == 404
        assert post(machine="hamilton", name="absent").status_code == 404
        r = post(machine="hamilton", name="no_images")
        assert r.status_code == 400 and "raw image logs" in r.text


def test_one_index_job_per_run(settings, fleet, monkeypatch):
    run = raw_run(fleet.root(HAMILTON_IP))
    fake = FakeIndexer(run)
    fake.release.clear()
    monkeypatch.setattr(maps_route, "stream_command", fake)
    h, _ = with_env_probe(fleet)
    c, svc = make_client(settings, helpers=h)
    with c:
        body = {"machine": "hamilton", "name": "map_x"}
        first = c.post("/api/maps/index_images", json=body).json()["job_id"]
        second = c.post("/api/maps/index_images", json=body)
        assert second.status_code == 409
        assert second.json()["detail"]["job_id"] == first
        fake.release.set()
        assert _wait(svc, first)["state"] == "succeeded"
        assert c.post("/api/maps/index_images", json=body).status_code == 200


def test_stream_command_logs_lines_and_returns_rc():
    import types

    logged = []
    job = types.SimpleNamespace(
        log=lambda lvl, t: logged.append(t), check_cancelled=lambda: None
    )
    rc, tail = maps_route.stream_command(
        job, ["sh", "-c", "echo one; echo two >&2; exit 3"], None
    )
    assert rc == 3 and logged == ["one", "two"] and tail == "one\ntwo"


# --- transfer of a run whose store was never built ------------------------------


def test_transfer_without_image_store_copies_map_and_warns(
    settings, fleet, monkeypatch
):
    raw_run(fleet.root(HAMILTON_IP))
    monkeypatch.setattr(
        maps_route,
        "pipe_store",
        lambda *a: (
            False,
            "pack failed (rc 1): FileNotFoundError: no image store at /x/image_store",
        ),
    )
    h, _ = with_env_probe(fleet)
    c, svc = make_client(settings, helpers=h)
    with c:
        body = {
            "src": "hamilton",
            "experiment": "map_x",
            "images": "keyframes",
            "compression": "fast",
        }
        job = _wait(svc, c.post("/api/maps/transfer", json=body).json()["job_id"])
        assert job["state"] == "succeeded", job["log"]
        assert job["result"]["image_store"] == "missing"
        assert any(
            line["level"] == "warn" and "Build image store" in line["text"]
            for line in job["log"]
        )
        assert (
            fleet.root() / "map_x__hamilton" / "hydra" / "backend" / "dsg.json"
        ).is_file()
