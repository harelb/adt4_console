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

from adt4_console import optional  # noqa: E402
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


def _needs(feature: str):
    return pytest.mark.skipif(
        not optional.available(feature),
        reason=f"needs {optional.FEATURES[feature][0]} ({feature})",
    )


# tests of features whose package may be missing (optional.FEATURES)
needs_fleet_core = _needs("fleet_core")
needs_fleetlink = _needs("fleetlink")
needs_images = _needs("image_store")
needs_map_edit = _needs("map_edit")
needs_rclpy = pytest.mark.skipif(
    optional.optional("rclpy.qos", None, "ROS 2") is None, reason="needs ROS 2 (rclpy)"
)
needs_lease_state = _needs("lease_state")
needs_action_results = _needs("action_results")
# heracles' Neo4j version counter (graph_interface.read_version)
needs_version_counter = pytest.mark.skipif(
    optional.optional("heracles.graph_interface", "read_version", "version counter")
    is None,
    reason="needs heracles.graph_interface.read_version",
)


def _helper(name: str) -> bool:
    try:
        from dcist_launch_system import fleet_helpers
    except ImportError:
        return False
    return hasattr(fleet_helpers, name)


# fleet tooling with fleetlink link modes / topology robot colors
needs_fleetlink_mode = pytest.mark.skipif(
    not _helper("fleetlink_port"), reason="needs fleet_helpers' fleetlink link mode"
)
needs_robot_colors = pytest.mark.skipif(
    optional.optional("dcist_launch_system.robot_colors", None, "robot colors") is None,
    reason="needs dcist_launch_system.robot_colors",
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
