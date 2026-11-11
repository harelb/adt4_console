import importlib.abc
import os
import pathlib
import shutil
import socket
import sys

import pytest


def _hide_packages(names: str) -> None:
    """Make packages (``pkg`` / ``pkg.mod``) or message classes
    (``pkg.msg.Name``) unimportable, to test the console without optional
    packages: ``ADT4_CONSOLE_TEST_HIDE=fleetlink,robot_executor_msgs.msg.RuntimeGuardsMsg``."""
    hidden = [n.strip() for n in names.split(",") if n.strip()]
    attrs = [n for n in hidden if n.rsplit(".", 1)[-1][:1].isupper()]
    mods = [n for n in hidden if n not in attrs]

    class _Hide(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if any(fullname == m or fullname.startswith(m + ".") for m in mods):
                raise ModuleNotFoundError(
                    f"No module named {fullname!r}", name=fullname
                )
            return None

    sys.meta_path.insert(0, _Hide())
    for a in attrs:
        mod, name = a.rsplit(".", 1)
        try:
            delattr(__import__(mod, fromlist=[name]), name)
        except (ImportError, AttributeError):
            pass


if os.environ.get("ADT4_CONSOLE_TEST_HIDE"):
    _hide_packages(os.environ["ADT4_CONSOLE_TEST_HIDE"])

from adt4_console.app import build_services, create_app  # noqa: E402
from adt4_console.fleet_service import FleetDeps  # noqa: E402
from adt4_console.settings import Settings  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
# a topology in Awesome-DCIST-T4's layout, with documentation addresses
TOPOLOGY = HERE / "fixtures" / "topology.yaml"
# integration tests: a real run directory and a Neo4j test instance (skipped
# when absent)
PENN = pathlib.Path(
    os.path.expanduser(os.environ.get("ADT4_CONSOLE_TEST_RUN", "/nonexistent"))
)
NEO4J_URI = os.environ.get("ADT4_CONSOLE_TEST_NEO4J", "neo4j://localhost:7697")
NEO4J_AUTH = (
    os.environ.get("HERACLES_NEO4J_USERNAME", "neo4j"),
    os.environ.get("HERACLES_NEO4J_PASSWORD", "neo4j_pw"),
)


def port_open(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def fake_fleet_deps(**over) -> FleetDeps:
    """Real topology parsing, no ping/ssh: every machine is online."""

    def filter_reachable(machines, **_):
        for m in machines:
            m.update(ping=True, ssh=True, online=True)
        return machines, []

    kw = dict(
        detect_network=lambda topo: "mit_wifi",
        filter_reachable=filter_reachable,
        get_remote_status=lambda user, ip, **_: {
            "tmux_sessions": "none",
            "load": "0/0/0",
        },
        get_local_ips=lambda: ["127.0.0.1"],
    )
    kw.update(over)
    return FleetDeps(**kw)


@pytest.fixture
def topology_copy(tmp_path):
    dst = tmp_path / "topology.yaml"
    shutil.copy(TOPOLOGY, dst)
    return dst


@pytest.fixture
def settings(tmp_path, topology_copy):
    return Settings(
        topology=str(topology_copy),
        network="mit_wifi",
        output_root=str(tmp_path / "out"),
        ros=False,
        fleetlink=False,
        fleet_poll=False,
        llm=False,
        static_dir=tmp_path / "static",
        confirm_timeout_s=5.0,
        sam3_url="http://127.0.0.1:9",  # nothing listens: tests never hit a real server
    )


def make_client(settings, **service_kw):
    from fastapi.testclient import TestClient

    service_kw.setdefault("fleet_deps", fake_fleet_deps())
    services = build_services(settings, **service_kw)
    services.fleet.refresh_reachability()
    return TestClient(create_app(settings, services)), services


@pytest.fixture
def client(settings):
    c, services = make_client(settings)
    with c:
        yield c
