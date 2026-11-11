"""Planner discovery uses the installed ROS share tree or a source checkout."""

import sys
import types

import pytest

from adt4_console import ros_bridge


def write_plugins(root, config, name, kind="Grstaps"):
    path = root / "config" / config / "omniplanner_plugins.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"planners:\n  {name}:\n    plugin:\n      type: {kind}\n")
    return path


@pytest.fixture
def installed_launch_package(tmp_path, monkeypatch):
    pytest.importorskip("ament_index_python.packages")
    prefix = tmp_path / "installed"
    marker = prefix / "share/ament_index/resource_index/packages/dcist_launch_system"
    marker.parent.mkdir(parents=True)
    marker.touch()
    monkeypatch.setenv("AMENT_PREFIX_PATH", str(prefix))
    monkeypatch.delenv("ADT4_OMNIPLANNER_PLUGINS", raising=False)
    module = types.ModuleType("dcist_launch_system")
    module.__file__ = str(
        prefix / "lib/python3.12/site-packages/dcist_launch_system/__init__.py"
    )
    monkeypatch.setitem(sys.modules, "dcist_launch_system", module)
    return prefix / "share/dcist_launch_system", module


def test_installed_session_config_is_discovered(installed_launch_package, monkeypatch):
    share, _ = installed_launch_package
    expected = write_plugins(share, "grstaps_repair", "running_grstaps")
    write_plugins(share, "default", "default_goto", "GotoPoints")
    monkeypatch.setenv("config", "grstaps_repair")
    assert ros_bridge.plugins_yaml_path() == expected
    assert ros_bridge.load_plugins()[0]["name"] == "running_grstaps"


def test_installed_config_falls_back_to_default(installed_launch_package, monkeypatch):
    share, _ = installed_launch_package
    expected = write_plugins(share, "default", "default_goto", "GotoPoints")
    monkeypatch.setenv("config", "missing_config")
    assert ros_bridge.plugins_yaml_path() == expected
    assert ros_bridge.load_plugins()[0]["kind"] == "goto"


def test_explicit_config_overrides_installed_share(
    installed_launch_package, tmp_path, monkeypatch
):
    share, _ = installed_launch_package
    write_plugins(share, "default", "installed")
    expected = write_plugins(tmp_path, "custom", "override")
    monkeypatch.setenv("ADT4_OMNIPLANNER_PLUGINS", str(expected))
    assert ros_bridge.plugins_yaml_path() == expected
    assert ros_bridge.load_plugins()[0]["name"] == "override"


def test_source_checkout_fallback(installed_launch_package, tmp_path, monkeypatch):
    from ament_index_python import packages

    _, module = installed_launch_package
    root = tmp_path / "checkout/dcist_launch_system"
    module.__file__ = str(root / "src/dcist_launch_system/__init__.py")
    expected = write_plugins(root, "grstaps_repair", "source_grstaps")
    monkeypatch.setenv("config", "grstaps_repair")

    def unavailable(name):
        raise LookupError(name)

    monkeypatch.setattr(packages, "get_package_share_directory", unavailable)
    assert ros_bridge.plugins_yaml_path() == expected
    assert ros_bridge.load_plugins()[0]["name"] == "source_grstaps"


def test_selected_installed_config_beats_source_default(
    installed_launch_package, tmp_path, monkeypatch
):
    share, module = installed_launch_package
    root = tmp_path / "checkout/dcist_launch_system"
    module.__file__ = str(root / "src/dcist_launch_system/__init__.py")
    write_plugins(root, "default", "source_default")
    expected = write_plugins(share, "grstaps_repair", "installed_selected")
    monkeypatch.setenv("config", "grstaps_repair")
    assert ros_bridge.plugins_yaml_path() == expected


def test_selected_source_config_beats_installed_copy(
    installed_launch_package, tmp_path, monkeypatch
):
    share, module = installed_launch_package
    root = tmp_path / "checkout/dcist_launch_system"
    module.__file__ = str(root / "src/dcist_launch_system/__init__.py")
    expected = write_plugins(root, "grstaps_repair", "source_selected")
    write_plugins(share, "grstaps_repair", "installed_copy")
    monkeypatch.setenv("config", "grstaps_repair")
    assert ros_bridge.plugins_yaml_path() == expected
