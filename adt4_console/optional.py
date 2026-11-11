"""Optional packages and the features they turn on.

The console runs against whichever workspace it is installed in. Some features
need packages (or newer versions of them) that a workspace may not have: each
is listed in :data:`FEATURES` and looked up here, so a missing one turns its
feature off with a single log line instead of breaking the console. The browser
reads :func:`features` from ``/api/health`` and hides what is off; a route that
needs a missing package answers 503 (:class:`FeatureUnavailable`, or the
``ImportError`` handler in ``app.py``).
"""

from __future__ import annotations

import importlib
import inspect
import logging
import threading

log = logging.getLogger(__name__)

# name -> (module, attribute or None, what it turns on)
FEATURES: dict[str, tuple[str, str | None, str]] = {
    "fleet_core": (
        "dcist_launch_system.fleet.launch_workflow",
        None,
        "launching, map transfers, preflight checks and the network tools "
        "(dcist_launch_system's fleet core)",
    ),
    "fleetlink": (
        "fleetlink.receiver",
        None,
        "the live robot link (graph, video, lidar, image mirror) over fleetlink",
    ),
    "map_edit": ("heracles.graph_edit", None, "editing Neo4j maps"),
    "scene_edit_tools": (
        "heracles_agents.scene_graph_edit",
        None,
        "plan repair's scene edits (SGET) in the chat",
    ),
    "pddl_planning": (
        "dsg_pddl.pddl_grounding",
        None,
        "in-console PDDL planning (mission preview, plan checks)",
    ),
    "grstaps": ("omniplanner.grstaps_planner", None, "GRSTAPS-X mission planning"),
    "plan_validity": (
        "omniplanner.plan_validity",
        None,
        "replan recommendations after scene changes",
    ),
    "image_store": (
        "hydra_image_store",
        None,
        "keyframe images, object crops and image search results",
    ),
    "action_results": (
        "robot_executor_msgs.msg",
        "ActionResultMsg",
        "per-action results from the robots' executors",
    ),
    "lease_state": (
        "robot_executor_msgs.msg",
        "RuntimeGuardsMsg",
        "the Spot lease indicator (from the executors' runtime guards)",
    ),
}

_lock = threading.Lock()
_cache: dict[tuple[str, str | None], object] = {}
_warned: set[str] = set()
_MISSING = object()


class FeatureUnavailable(RuntimeError):
    """A route needs a feature this workspace does not have (-> 503)."""

    def __init__(self, feature: str, detail: str):
        super().__init__(detail)
        self.feature = feature
        self.detail = detail


def _lookup(module: str, name: str | None):
    key = (module, name)
    with _lock:
        if key in _cache:
            return _cache[key]
    try:
        obj = importlib.import_module(module)
        if name is not None:
            obj = getattr(obj, name)
    except Exception as e:  # noqa: BLE001 — ImportError, AttributeError, a broken package
        obj = _MISSING
        _cache_err[key] = f"{type(e).__name__}: {e}"
    with _lock:
        _cache[key] = obj
    return obj


_cache_err: dict[tuple[str, str | None], str] = {}


def _warn_once(key: str, msg: str, *args) -> None:
    with _lock:
        if key in _warned:
            return
        _warned.add(key)
    log.warning(msg, *args)


def optional(module: str, name: str | None, feature: str):
    """``module.name`` (or the module when ``name`` is None), or None when it is
    missing; logged once per name: ``feature`` is off."""
    obj = _lookup(module, name)
    if obj is _MISSING:
        what = f"{module}.{name}" if name else module
        _warn_once(
            what,
            "%s is not available (%s): %s is off",
            what,
            _cache_err.get((module, name), "missing"),
            feature,
        )
        return None
    return obj


def available(feature: str) -> bool:
    module, name, what = FEATURES[feature]
    return optional(module, name, what) is not None


def require(feature: str):
    """The feature's module / attribute, or raise :class:`FeatureUnavailable`."""
    module, name, what = FEATURES[feature]
    obj = optional(module, name, what)
    if obj is None:
        pkg = f"{module}.{name}" if name else module
        raise FeatureUnavailable(
            feature, f"not available in this workspace: {what} (needs {pkg})"
        )
    return obj


def features() -> dict[str, bool]:
    """{feature: available} for every entry of :data:`FEATURES`."""
    return {f: available(f) for f in FEATURES}


def reset() -> None:
    """Forget lookups (tests that install / remove fakes)."""
    with _lock:
        _cache.clear()
        _cache_err.clear()
        _warned.clear()


def call_accepting(fn, *args, **kwargs):
    """``fn(*args, **kwargs)`` without the keyword arguments ``fn`` does not take
    (older package versions take fewer options)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(*args, **kwargs)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(*args, **kwargs)
    return fn(*args, **{k: v for k, v in kwargs.items() if k in params})
