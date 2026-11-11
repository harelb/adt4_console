"""``dcist_launch_system.fleet_helpers`` functions the console uses that older
workspaces lack, with the fallbacks used when they are missing.

The fleet tooling grows in Awesome-DCIST-T4 (platform rule, fleetlink link
modes); the console asks for each piece here instead of failing on an
``AttributeError`` or an unexpected keyword.
"""

from __future__ import annotations

import logging

from .optional import call_accepting  # noqa: F401 (fleet_service)

log = logging.getLogger(__name__)

_warned: set[str] = set()


def helpers():
    from dcist_launch_system import fleet_helpers

    return fleet_helpers


def _get(name: str, why: str):
    fn = getattr(helpers(), name, None)
    if fn is None and name not in _warned:
        _warned.add(name)
        log.info("dcist_launch_system.fleet_helpers has no %s: %s", name, why)
    return fn


def platform_of(machine: dict | None) -> str:
    """A robot's platform by the shared rule: topology ``platform_type``; a
    missing one is spot; husky is phoenix."""
    fn = _get("platform_of", "using the built-in platform rule")
    if fn is not None:
        return fn(machine)
    ptype = str((machine or {}).get("platform_type") or "spot").strip().lower()
    return "phoenix" if ptype == "husky" else ptype


def status_platform(machine: dict | None, topology: dict | None = None):
    """``platform_type`` for status polling: a robot's platform (its topology
    entry first), None for any other machine."""
    fn = _get("status_platform", "using the built-in platform rule")
    if fn is not None:
        return fn(machine, topology)
    if (machine or {}).get("role") != "robot":
        return None
    entry = ((topology or {}).get("machines") or {}).get((machine or {}).get("name"))
    return platform_of(entry or machine)


# ---------------------------------------------------------------- link modes
# Without fleetlink support in the fleet tooling there is one link mode: the
# zenoh router mesh.


def link_modes() -> tuple[str, ...]:
    return tuple(getattr(helpers(), "FLEET_LINK_MODES", ("router",)))


def fleet_link_mode(topo: dict) -> str:
    fn = _get("fleet_link_mode", "router mode only")
    return fn(topo) if fn is not None else "router"


def fleetlink_base(topo: dict):
    fn = _get("fleetlink_base", "no fleetlink base station")
    return fn(topo) if fn is not None else None


def link_network(topo: dict, requested, active):
    fn = _get("link_network", "robot links use the active network")
    return fn(topo, requested, active) if fn is not None else (requested or active)
