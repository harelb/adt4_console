"""Launch / robot control (API.md §5) — wraps ``fleet.launch_workflow``."""

from __future__ import annotations

import logging
import pathlib
import re
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..jobs import JobLaunchCallbacks
from ..ros_bridge import RosUnavailable
from . import services

router = APIRouter(prefix="/api")
log = logging.getLogger(__name__)


class LaunchPlanBody(BaseModel):
    # empty with base_only: restart only the base station's session
    robots: list[str] = []
    # one session for every robot (old clients; a per-platform preset maps to
    # each robot's platform); ``sessions`` overrides it per platform
    session: str = ""
    experiment: str
    # platform ("spot" | "phoenix") -> robot session for that platform's robots
    sessions: dict[str, str] = {}
    # launch only the base station (robots are ignored); remembered by the form
    base_only: bool = False
    # (re)launch only ``robots``: the base station's session, zenoh config and
    # fleetlink router/bridge are not touched (``base_station`` is only the
    # reference for the checks); a robot's running session is stopped cleanly
    robots_only: bool = False
    # robots only: Stop & save each robot first (hydra / ROMAN save, then teardown)
    save_first: bool = False
    mode: str | None = None
    prior_path: str = ""
    prior_source: str | None = None
    record: bool = False
    base_station: str | None = None
    bs_session: str | None = None
    # robot <-> base link network (a topology network name); None = topology default
    link_network: str | None = None
    # "router" | "fleetlink" for this launch; None = fleet_defaults.fleet_link
    link_mode: str | None = None
    # pre-launch checks; a NO-GO asks to confirm each issue (fleet.preflight)
    preflight: bool = True


def _with_link_mode(topo: dict, mode: str | None) -> dict:
    """``launch_workflow.with_link_mode``; without it (fleet tooling with no
    fleetlink support) only the router mode exists."""
    from dcist_launch_system.fleet import launch_workflow as lw

    fn = getattr(lw, "with_link_mode", None)
    if fn is not None:
        return fn(topo, mode)
    if mode not in (None, "router"):
        raise lw.LaunchError(f"link mode {mode!r} is not supported by this workspace")
    return topo


def _plan(
    body: LaunchPlanBody, topo: dict | None = None, runtime_config: dict | None = None
):
    import dataclasses

    from dcist_launch_system.fleet.launch_workflow import LaunchError, LaunchPlan

    if body.link_network and body.link_network not in (
        (topo or {}).get("networks") or {}
    ):
        raise HTTPException(400, f"unknown link network {body.link_network!r}")
    if body.link_mode:
        try:
            _with_link_mode(topo or {}, body.link_mode)
        except LaunchError as e:
            raise HTTPException(400, str(e)) from None
    fields = body.model_dump()
    fields.pop("save_first", None)
    if body.base_only and body.robots_only:
        raise HTTPException(400, "Pick either base station only or robots only")
    if body.base_only:
        if not (body.base_station and body.bs_session):
            raise HTTPException(
                400, "Base station only: pick a base station and its session"
            )
        fields.update(robots=[], session="", sessions={})
    if body.robots_only:
        if not body.robots:
            raise HTTPException(400, "Robots only: pick the robot(s) to (re)launch")
        fields.update(bs_session=None)
    fields["sessions"] = {p: s for p, s in (fields.get("sessions") or {}).items() if s}
    if runtime_config is not None:
        fields["sessions"] = resolve_platform_sessions(fields, topo, runtime_config)
    if dataclasses.is_dataclass(LaunchPlan):
        # older fleet tooling plans have fewer options (no link network / mode,
        # no per-platform sessions)
        known = {f.name for f in dataclasses.fields(LaunchPlan)}
        if body.robots_only and "robots_only" not in known:
            raise HTTPException(
                400,
                "Robots only: this workspace's fleet tooling cannot relaunch robots alone",
            )
        if "sessions" not in known and not fields["session"] and fields["sessions"]:
            fields["session"] = next(iter(fields["sessions"].values()))
        fields = {k: v for k, v in fields.items() if k in known}
    return LaunchPlan(**fields)


def _validate(plan, runtime_config, output_root, topo=None):
    """``launch_workflow.validate_plan`` (base-station-only aware); older fleet
    tooling has only ``build_robot_commands`` (robots required).  A robot session
    of the other launch mode is refused either way (:func:`check_session_modes`)."""
    from dcist_launch_system.fleet import launch_workflow as lw

    fn = getattr(lw, "validate_plan", None)
    if fn is not None:
        cmds = fn(plan, runtime_config, output_root, topo)
    else:
        cmds = lw.build_robot_commands(plan, runtime_config, output_root)
    check_session_platforms(plan, runtime_config, topo)
    check_session_modes(plan, runtime_config, topo)
    return cmds


def session_platform(session: str | None) -> str | None:
    """``"spot"`` | ``"phoenix"`` | ``None``: the platform a robot session is for
    (``launch_workflow.session_platform``, else by its experiment name)."""
    from dcist_launch_system.fleet import launch_workflow as lw

    fn = getattr(lw, "session_platform", None)
    if fn is not None:
        return fn(session)
    exp = str(session or "").partition("-")[0]
    return next((p for p in ("spot", "phoenix") if exp.startswith(p)), None)


def platform_equivalent(session, platform, topo, mode, available) -> str | None:
    """``session`` for a robot of ``platform``: itself when it fits, else the
    other platform's twin (spot_relocalize-X <-> phoenix_relocalize-X) when that
    session exists, else the platform's preset for the mode; None without one."""
    own = session_platform(session)
    if own is None or own == platform:
        return session
    swapped = platform + str(session)[len(own) :]
    if swapped in available:
        return swapped
    presets = ((topo or {}).get("fleet_defaults") or {}).get("sessions") or {}
    preset = (presets.get(session_mode(session) or mode) or {}).get(platform)
    return preset if preset and session_platform(preset) in (None, platform) else None


def resolve_platform_sessions(
    fields: dict, topo: dict | None, runtime_config: dict
) -> dict:
    """Fill ``sessions`` (platform -> session) for every selected robot's platform
    from a single ``session`` (older clients, legacy launch memory): a robot never
    gets another platform's session (2026-11-11: gauss, a phoenix, was sent the
    Spot session spot_relocalize-relocalize_awcd).  A platform without an
    equivalent is left out (the launch then refuses it)."""
    from dcist_launch_system.fleet.launch_workflow import list_sessions

    from .. import fleet_compat as fc

    session = fields.get("session") or ""
    sessions = dict(fields.get("sessions") or {})
    if not session or not fields.get("robots"):
        return sessions
    machines = (topo or {}).get("machines") or {}
    available = None
    for r in fields["robots"]:
        if r not in runtime_config:
            continue
        p = fc.platform_of(machines.get(r) or runtime_config[r])
        if sessions.get(p):
            continue
        if available is None:
            available = set(list_sessions())
        s = platform_equivalent(session, p, topo, fields.get("mode"), available)
        if s:
            sessions[p] = s
    return sessions


def check_session_platforms(plan, runtime_config: dict, topo: dict | None) -> None:
    """Raise ``LaunchError`` for a robot session of another platform (a phoenix
    robot running spot_*, a Spot running phoenix_*): ``launch_workflow.
    check_session_platforms`` when the fleet tooling has it."""
    from dcist_launch_system.fleet import launch_workflow as lw

    from .. import fleet_compat as fc

    fn = getattr(lw, "check_session_platforms", None)
    if fn is not None:
        return fn(plan, runtime_config, topo)
    robot_session = getattr(lw, "robot_session", None)
    machines = (topo or {}).get("machines") or {}
    bad = []
    for r in plan.robots:
        if r not in runtime_config:
            continue
        s = (
            robot_session(plan, runtime_config[r], topo)
            if robot_session
            else plan.session
        )
        sp, rp = (
            session_platform(s),
            fc.platform_of(machines.get(r) or runtime_config[r]),
        )
        if sp and sp != rp:
            bad.append(f"{r} (platform {rp}) would run {s} (a {sp} session)")
    if bad:
        raise lw.LaunchError(
            "Session does not match the robot's platform: "
            + "; ".join(bad)
            + ". Pick a session of each robot's platform."
        )


_RELOCALIZE = re.compile(r"(^|_)(bag_)?relocalize$")


def session_mode(session: str | None) -> str | None:
    """``"relocalize"`` | ``"mapping"`` | ``None`` for a session file name:
    ``launch_workflow.session_mode`` (from experiment_manifest.yaml), else, on
    fleet tooling without it, by name (``<experiment>-<config>``: a spot*/phoenix*
    experiment ending in ``relocalize`` relocalizes, other spot*/phoenix* ones map,
    except the prior-map planning sims)."""
    from dcist_launch_system.fleet import launch_workflow as lw

    fn = getattr(lw, "session_mode", None)
    if fn is not None:
        return fn(session)
    exp, sep, _ = (session or "").partition("-")
    if not sep or not exp.startswith(("spot", "phoenix")):
        return None
    if _RELOCALIZE.search(exp):
        return "relocalize"
    if "prior_dsg" in exp or "grstaps" in exp:
        return None
    return "mapping"


def session_modes(sessions) -> dict[str, str]:
    """session -> mode for the robot sessions among ``sessions`` (the Launch
    form's pickers list the selected mode's)."""
    out = {}
    for s in sessions:
        m = session_mode(s)
        if m:
            out[s] = m
    return out


def check_session_modes(plan, runtime_config: dict, topo: dict | None) -> None:
    """Raise ``LaunchError`` for a robot session of the other launch mode (a
    relocalize launch running a mapping session never loads the prior):
    ``launch_workflow.check_session_modes`` when the fleet tooling has it."""
    from dcist_launch_system.fleet import launch_workflow as lw

    fn = getattr(lw, "check_session_modes", None)
    if fn is not None:
        return fn(plan, runtime_config, topo)
    mode = getattr(plan, "mode", None)
    if mode not in ("mapping", "relocalize"):
        return
    robot_session = getattr(lw, "robot_session", None)
    bad = []
    for r in plan.robots:
        if r not in runtime_config:
            continue
        s = (
            robot_session(plan, runtime_config[r], topo)
            if robot_session
            else plan.session
        )
        m = session_mode(s)
        if m and m != mode:
            bad.append(f"{r} would run {s} (a {m} session)")
    if bad:
        raise lw.LaunchError(
            f"Launch mode is {mode} but " + "; ".join(bad) + f". Pick a {mode} session."
        )


def platform_choices(topo: dict | None, runtime_config: dict, mode: str) -> dict:
    """Each robot's platform (spot | phoenix) and the mode's preset session per
    platform (``fleet_defaults.sessions``), for the per-platform session pickers."""
    from .. import fleet_compat as fc

    machines = (topo or {}).get("machines") or {}
    presets = (((topo or {}).get("fleet_defaults") or {}).get("sessions") or {}).get(
        mode
    ) or {}
    return {
        "robot_platforms": {
            n: fc.platform_of(machines.get(n) or m)
            for n, m in runtime_config.items()
            if m.get("role") == "robot"
        },
        "platform_sessions": {p: s for p, s in presets.items() if s},
    }


def link_choices(topo: dict | None, active: str | None) -> dict:
    """Networks a launch can put the robot links on, and the default one."""
    from .. import fleet_compat as fc

    return {
        "link_networks": list(((topo or {}).get("networks") or {}).keys()),
        "link_network": fc.link_network(topo or {}, None, active),
        "active_network": active,
        "link_modes": list(fc.link_modes()),
        "link_mode": fc.fleet_link_mode(topo or {}),
        "fleetlink_base": fc.fleetlink_base(topo or {}),
    }


@router.get("/launch/defaults")
def launch_defaults(request: Request, mode: str = "mapping"):
    from dcist_launch_system.fleet.launch_workflow import (
        default_selection,
        list_sessions,
    )

    svc = services(request)
    fleet = svc.fleet
    with fleet.lock:
        sel = default_selection(
            fleet.topo, fleet.runtime_config, mode, None, fleet.local_ips
        )
        links = link_choices(fleet.topo, fleet.network)
        platforms = platform_choices(fleet.topo, fleet.runtime_config, mode)
    sessions = list_sessions()
    return {
        **sel,
        **links,
        **platforms,
        "mode": mode,
        "sessions": sessions,
        # session -> "mapping" | "relocalize" (robot sessions only): the robot
        # session pickers list the selected mode's
        "session_modes": session_modes(sessions),
        "last": last_launch(svc, sel.get("base_station")),
    }


def last_launch(svc, base_station: str | None = None) -> dict | None:
    """The Launch form's last choices for ``base_station`` (prior map, sessions,
    robots, ...), else those of the active launch record."""
    from ..launch_state import memory_from_active

    mem = getattr(svc, "launch_memory", None)
    last = mem.get(base_station) if mem is not None else None
    if last is None and svc.launch_state is not None:
        last = memory_from_active(svc.launch_state.get())
    return last


@router.get("/launch/last")
def launch_last(request: Request, base_station: str | None = None):
    return {"last": last_launch(services(request), base_station)}


@router.post("/launch/dry_run")
def launch_dry_run(body: LaunchPlanBody, request: Request):
    from dcist_launch_system.fleet.launch_workflow import LaunchError, dry_run_lines

    svc = services(request)
    try:
        with svc.fleet.lock:
            plan = _plan(body, svc.fleet.topo, svc.fleet.runtime_config)
            lines = dry_run_lines(
                plan,
                svc.fleet.runtime_config,
                svc.settings.output_root,
                svc.fleet.topo,
                svc.fleet.network,
            )
            check_session_platforms(plan, svc.fleet.runtime_config, svc.fleet.topo)
            check_session_modes(plan, svc.fleet.runtime_config, svc.fleet.topo)
    except LaunchError as e:
        raise HTTPException(400, str(e)) from None
    return {"lines": lines}


@router.post("/launch/preflight")
def launch_preflight(body: LaunchPlanBody, request: Request):
    """Run the pre-launch checks on this plan's machines now (read-only ssh).

    The prior map is verified during the launch itself, after it is copied."""
    from dcist_launch_system.fleet import preflight as pf
    from dcist_launch_system.fleet.launch_workflow import LaunchDeps

    svc = services(request)
    fleet = svc.fleet
    with fleet.lock:
        topo = fleet.topo
        runtime_config = {k: dict(v) for k, v in fleet.runtime_config.items()}
    plan = _plan(body, topo, runtime_config)
    unknown = [r for r in plan.robots if r not in runtime_config]
    if unknown:
        raise HTTPException(400, f"unknown robot(s): {', '.join(unknown)}")
    machines = [runtime_config[r] for r in plan.robots]
    bs = runtime_config.get(plan.base_station) if plan.base_station else None
    if bs and bs["name"] not in plan.robots and not body.robots_only:
        machines.append(bs)
    checks = pf.run_checks(
        machines,
        _with_link_mode(topo, getattr(plan, "link_mode", None)),
        svc.settings.output_root,
        svc.launch_deps or LaunchDeps(),
        reference=bs["name"] if bs else None,
    )
    return {
        "checks": [c.to_dict() for c in checks],
        "verdicts": pf.verdicts(checks),
        "nogo": len(pf.nogos(checks)),
        "t": time.time(),
    }


@router.post("/launch")
def launch(body: LaunchPlanBody, request: Request):
    from dcist_launch_system.fleet.launch_workflow import LaunchError, run_launch

    svc = services(request)
    fleet = svc.fleet
    with fleet.lock:
        plan = _plan(body, fleet.topo, fleet.runtime_config)
    try:  # validate up front so the UI gets a 400, not a failed job
        with fleet.lock:
            _validate(plan, fleet.runtime_config, svc.settings.output_root, fleet.topo)
    except LaunchError as e:
        raise HTTPException(400, str(e)) from None
    # the next launch (a relaunch after Stop & save) starts from these choices,
    # prior map included; a robots-only relaunch keeps the full launch's robots
    # and base station session
    if getattr(svc, "launch_memory", None) is not None:
        remember_launch(svc.launch_memory, body, plan)
    if body.robots_only:
        log.info(
            "launch: robots only %s (experiment %s, sessions %s, save_first=%s); "
            "base station %s not touched",
            ",".join(plan.robots), plan.experiment, plan.sessions or plan.session,
            body.save_first, plan.base_station,
        )  # fmt: skip

    def run(job):
        cb = JobLaunchCallbacks(job, fleet.zenohd_state)
        if body.robots_only:
            # the operator asked to restart these robots: their running session
            # is stopped without a second question
            cb = _AutoStop(cb)
            if body.save_first:
                for r in plan.robots:
                    job.log("heading", f"Stop & save {r} before the relaunch ...")
                    res = stop_save_run(svc, job, r, teardown=True)
                    if res is False:
                        job.log(
                            "error", f"ABORT: Stop & save failed on {r}; not relaunched"
                        )
                        return False
                    if isinstance(res, dict) and res.get("teardown") is False:
                        # killing the session now would cut hydra's save short
                        job.log(
                            "error",
                            f"ABORT: {r} is still saving (or its session did not stop); "
                            "relaunch it once the save is done",
                        )
                        return False
        with fleet.lock:
            topo = fleet.topo
            network = fleet.network
            runtime_config = {k: dict(v) for k, v in fleet.runtime_config.items()}
        ok = run_launch(
            plan,
            topo=topo,
            active_network=network,
            runtime_config=runtime_config,
            output_root=svc.settings.output_root,
            callbacks=cb,
            deps=svc.launch_deps,
        )
        if ok and body.robots_only:
            _robots_rejoined(svc, plan)
        elif ok:
            try:
                attach_prior(svc, plan, job)
            except Exception as e:  # noqa: BLE001 - the launch itself succeeded
                job.log("warn", f"could not register the prior map in the viewer: {e}")
        return ok

    if body.robots_only:
        title = f"Relaunch {', '.join(plan.robots)} ({plan.experiment}), robots only"
    elif plan.robots:
        title = f"Launch {plan.experiment} on {', '.join(plan.robots)}"
    else:
        title = f"Launch {plan.experiment}: base station {plan.base_station} only"
    job = svc.jobs.start("launch", title, run)
    return {"job_id": job.id}


class _AutoStop:
    """Launch callbacks that answer the "stop the running session" question yes
    (a robots-only relaunch the operator asked for); every other one is asked."""

    def __init__(self, cb):
        self._cb = cb

    def __getattr__(self, name):
        return getattr(self._cb, name)

    def confirm(self, request):
        if getattr(request, "kind", None) == "kill_sessions":
            machines = (getattr(request, "detail", None) or {}).get("machines") or []
            self._cb.log(
                "dim",
                f"stopping the running session on {', '.join(machines)} (restart)",
            )
            return True
        return self._cb.confirm(request)


def remember_launch(memory, body: LaunchPlanBody, plan) -> dict:
    """Remember the Launch form's choices; a robots-only relaunch only adds its
    robots and their platforms' sessions to the last full launch."""
    fields = body.model_dump()
    if body.robots_only:
        prev = memory.get(body.base_station) or {}
        robots = list(prev.get("robots") or [])
        robots += [r for r in plan.robots if r not in robots]
        fields.update(
            robots=robots,
            session=prev.get("session") or body.session,
            sessions={**(prev.get("sessions") or {}), **(plan.sessions or {})},
            bs_session=prev.get("bs_session"),
            base_only=False,
            experiment=prev.get("experiment") or body.experiment,
        )
    return memory.remember(fields)


def _robots_rejoined(svc, plan) -> None:
    """A robots-only relaunch: the active launch record keeps its prior and base
    and lists the relaunched robots too."""
    from ..launch_state import new_state

    if svc.launch_state is None:
        return
    prev = svc.launch_state.get()
    if not prev:
        svc.launch_state.set(new_state(plan, None, None))
        return
    robots = list(prev.get("robots") or [])
    robots += [r for r in plan.robots if r not in robots]
    svc.launch_state.set({**prev, "robots": robots})


def attach_prior(svc, plan, job) -> dict | None:
    """After a launch: remember it, and show its prior map in the viewer.

    The prior is registered from this machine's copy (the prior itself when it lives
    here, else the local run with the same run_id).  Without a local copy the launch
    record keeps ``pending``; transferring the prior later registers it.
    """
    from dcist_launch_system.fleet import run_identity as ri

    from ..launch_state import new_state, register_prior
    from .maps import _endpoint, _is_local, _local_name, _scan_one, find_local_copy

    if svc.launch_state is None:
        return None
    previous = svc.launch_state.get()
    if not plan.prior_path:
        svc.launch_state.set(new_state(plan, None, previous))
        return None
    src_name = plan.prior_source or _local_name(svc)
    src = _endpoint(svc, src_name)
    p = pathlib.PurePosixPath(plan.prior_path)
    entry = _scan_one(svc, src, p.name, str(p.parent))
    if entry is None:
        job.log(
            "warn", f"prior {src_name}:{plan.prior_path} not found; viewer not updated"
        )
        svc.launch_state.set(new_state(plan, None, previous))
        return None
    ident = entry.get("identity") or {}
    origin = entry.get("origin") or {}
    run_id = ri.canonical_id(entry)
    robot = (
        ident.get("robot")
        or origin.get("robot")
        or origin.get("source_machine")
        or src_name
    )
    experiment = ident.get("experiment") or origin.get("experiment") or p.name
    prior = {
        "run_id": run_id,
        "source_machine": src_name,
        "path": plan.prior_path,
        "experiment": experiment,
        "robot": robot,
        "label": f"prior: {experiment} ({robot})",
        "local_path": None,
        "source_id": None,
        "pending": True,
    }
    if _is_local(svc, src):
        prior["local_path"] = entry["abs_path"]
    else:
        local = find_local_copy(svc, run_id) if run_id else None
        if local is not None:
            prior["local_path"] = local["path"]
    if prior["local_path"]:
        prior["source_id"] = register_prior(svc.graph, prior)
        prior["pending"] = prior["source_id"] is None
    svc.launch_state.set(new_state(plan, prior, previous))
    if prior["source_id"]:
        svc.graph.notify_version(prior["source_id"], "registered")
        job.log(
            "ok",
            f"viewer: {prior['label']} from {prior['local_path']} + live "
            + ", ".join(svc.launch_state.get()["robots"]),
        )
    else:
        job.log(
            "warn",
            f"prior not on this machine yet: copy {src_name}:{plan.prior_path} here "
            "(Maps → Transfer to base) and the viewer will show it",
        )
    return prior


@router.get("/launch/active")
def launch_active(request: Request):
    """The last successful launch + the viewer's default sources for it."""
    from ..launch_state import default_sources

    svc = services(request)
    state = svc.launch_state.get() if svc.launch_state is not None else None
    return {"active": state, "default_sources": default_sources(state)}


@router.delete("/launch/active")
def launch_active_clear(request: Request):
    svc = services(request)
    if svc.launch_state is not None:
        svc.launch_state.clear()
    return {"active": None, "default_sources": []}


# Stop & save calls the robot stack's two map savers, in this order (the core
# window's shutdown pane runs the same pair, core.yaml).  hydra_ros_node's
# ~/shutdown (ianvs spinAndWait) only flags the node to exit and answers at
# once; hydra then saves the DSG + mesh (minutes with a big mesh) and exits.
# roman/shutdown pickles the ROMAN map before it answers.
HYDRA_SHUTDOWN_TIMEOUT_S = 15.0
ROMAN_SHUTDOWN_TIMEOUT_S = 180.0  # > fleetlink relay timeout_s (170) + 2
HYDRA_EXIT_WAIT_S = 600.0
HYDRA_EXIT_POLL_S = 2.0
# teardown: after Ctrl-C, how long nodes get to exit before the session is killed
TEARDOWN_WAIT_S = 30.0


def _wait_hydra_exit(svc, job, robot: str) -> bool | None:
    """Router mode: hydra's shutdown service disappears once hydra saved and
    exited.  True = gone, False = still up after HYDRA_EXIT_WAIT_S, None = cannot
    tell (no graph query)."""
    name = f"/{robot}/hydra/shutdown"
    has = getattr(svc.ros, "has_service", None)
    if has is None:
        return None
    t0 = time.monotonic()
    last_note = t0
    while time.monotonic() - t0 < HYDRA_EXIT_WAIT_S:
        try:
            if not has(name):
                return True
        except Exception:  # noqa: BLE001
            return None
        if time.monotonic() - last_note >= 30:
            last_note = time.monotonic()
            job.log("dim", f"hydra still saving ({time.monotonic() - t0:.0f} s) ...")
        time.sleep(HYDRA_EXIT_POLL_S)
    return False


# "[h]" keeps pgrep -f from matching the remote shell running this very command
# (its command line contains the pattern), which made hydra look up forever
HYDRA_PGREP = "pgrep -f '[h]ydra_ros_node' >/dev/null && echo up || echo gone"


def _wait_hydra_exit_ssh(svc, job, m) -> bool | None:
    """Like _wait_hydra_exit, by looking for the hydra process on the robot."""
    t0 = time.monotonic()
    last_note = t0
    while time.monotonic() - t0 < HYDRA_EXIT_WAIT_S:
        rc, out, _ = svc.helpers.ssh_cmd(m["user"], m["ip"], HYDRA_PGREP)
        if rc != 0 or out.strip() not in ("up", "gone"):
            return None
        if out.strip() == "gone":
            return True
        if time.monotonic() - last_note >= 30:
            last_note = time.monotonic()
            job.log("dim", f"hydra still saving ({time.monotonic() - t0:.0f} s) ...")
        time.sleep(HYDRA_EXIT_POLL_S)
    return False


def _teardown_stack(svc, job, robot: str, m) -> bool:
    """Ctrl-C every pane of the ADT4 session, give the nodes TEARDOWN_WAIT_S to
    exit, then kill the session."""
    from dcist_launch_system.fleet.launch_workflow import TMUX_SESSION_NAME

    from ..terminal import stop_session

    session = TMUX_SESSION_NAME
    job.log(
        "info",
        f"Stopping the rest of the stack on {robot} (Ctrl-C every pane of {session}) ...",
    )
    ok = stop_session(
        svc.helpers.ssh_cmd, m["user"], m["ip"], session,
        lambda lvl, text: job.log(lvl, f"{robot}: {text}"),
        wait_s=TEARDOWN_WAIT_S, poll_s=HYDRA_EXIT_POLL_S,
    )  # fmt: skip
    if ok:
        job.log("ok", f"{robot}: {session} stopped and closed")
    return ok


class StopSaveBody(BaseModel):
    # after saving, Ctrl-C and kill the robot's whole ADT4 tmux session
    teardown: bool = True


def stop_save_run(svc, job, robot: str, teardown: bool = True):
    """Stop & save ``robot`` inside ``job``: hydra / ROMAN save their maps, then
    (``teardown``) the robot's ADT4 session is stopped.  The job result: False on
    failure, else a dict (``teardown`` False when the stack was left running)."""

    def finish(job, result, hydra_exited):
        """Tear the stack down once hydra's save is known to be done."""
        if not teardown or result is False:
            return result
        m = svc.fleet.machine(robot)
        if m is None:
            job.log(
                "warn", f"{robot} is not in the topology; cannot stop its tmux session"
            )
            return result
        if hydra_exited is None:
            job.log("info", "Waiting for hydra to finish saving and exit ...")
            hydra_exited = _wait_hydra_exit_ssh(svc, job, m)
            if hydra_exited:
                job.log("ok", f"{robot}: hydra saved and exited")
        if hydra_exited is False:
            job.log(
                "warn",
                f"{robot}: hydra is still saving; left the stack running (kill it from Fleet when it is done)",
            )
            return {**result, "teardown": False}
        if hydra_exited is None:
            job.log(
                "warn",
                f"{robot}: cannot tell whether hydra finished saving; left the stack running",
            )
            return {**result, "teardown": False}
        return {**result, "teardown": _teardown_stack(svc, job, robot, m)}

    def run(job):
        if svc.ros.available:
            fleetlink = svc.ros.link_status(robot) is not None
            hydra = f"/{robot}/hydra/shutdown"
            roman = f"/{robot}/roman/shutdown"
            job.log("info", f"Calling {hydra} ...")
            try:
                svc.ros.call_service(
                    hydra, "std_srvs/srv/Empty", {}, timeout=HYDRA_SHUTDOWN_TIMEOUT_S
                )
            except Exception as e:  # noqa: BLE001
                job.log("warn", f"{hydra} failed: {e}")
            else:
                job.log(
                    "ok",
                    f"{robot}: hydra is shutting down; it saves the scene graph and "
                    "mesh before it exits (can take minutes with a big mesh)",
                )
                if fleetlink:
                    job.log(
                        "dim",
                        "fleetlink mode: the relay answers an Empty call even when the "
                        "robot did not; confirm on the robot's node status",
                    )
                job.log(
                    "info", f"Calling {roman} (saves the ROMAN map, then answers) ..."
                )
                roman_ok = True
                try:
                    svc.ros.call_service(
                        roman,
                        "std_srvs/srv/Empty",
                        {},
                        timeout=ROMAN_SHUTDOWN_TIMEOUT_S,
                    )
                    job.log("ok", f"{robot}: ROMAN map saved")
                except Exception as e:  # noqa: BLE001
                    roman_ok = False
                    job.log(
                        "warn",
                        f"{roman} failed: {e} (ROMAN may not run in this experiment)",
                    )
                hydra_saved = None
                if not fleetlink:  # the bridge serves relayed services forever
                    job.log("info", "Waiting for hydra to finish saving and exit ...")
                    hydra_saved = _wait_hydra_exit(svc, job, robot)
                    if hydra_saved:
                        job.log("ok", f"{robot}: hydra saved and exited")
                    elif hydra_saved is False:
                        job.log(
                            "warn",
                            f"{robot}: hydra still running after "
                            f"{HYDRA_EXIT_WAIT_S:.0f} s; check its pane",
                        )
                return finish(
                    job,
                    {
                        "method": "service",
                        "hydra": True,
                        "roman": roman_ok,
                        "hydra_exited": hydra_saved,
                    },
                    hydra_saved,
                )
        else:
            job.log("dim", "ROS unavailable; using tmux fallback")
        m = svc.fleet.machine(robot)
        if m is None:
            job.log(
                "error", f"{robot} is not in the topology; cannot fall back to tmux"
            )
            return False
        job.log("info", f"tmux fallback: running the core shutdown pane on {robot} ...")
        ok, msg = svc.helpers.stop_save_via_tmux(m["user"], m["ip"], robot=robot)
        job.log("ok" if ok else "error", f"{robot}: {msg}")
        return finish(job, {"method": "tmux", "detail": msg}, None) if ok else False

    return run(job)


@router.post("/robots/{robot}/stop_save")
def stop_save(robot: str, request: Request, body: StopSaveBody | None = None):
    svc = services(request)
    teardown = body.teardown if body else True
    job = svc.jobs.start(
        "stop_save",
        f"Stop & save {robot}",
        lambda job: stop_save_run(svc, job, robot, teardown),
    )
    return {"job_id": job.id}


class SaveDsgBody(BaseModel):
    path: str = ""
    include_mesh: bool = True


@router.post("/robots/{robot}/save_dsg")
def save_dsg(robot: str, request: Request, body: SaveDsgBody | None = None):
    svc = services(request)
    body = body or SaveDsgBody()
    if not svc.ros.available:
        raise RosUnavailable(svc.ros.error or "not running")
    service = f"/{robot}/dsg_saver/save_dsg"

    def run(job):
        job.log("info", f"Calling {service} (include_mesh={body.include_mesh}) ...")
        res = svc.ros.call_service(
            service,
            "dcist_launch_system_msgs/srv/SaveDsg",
            {"save_path": body.path, "include_mesh": body.include_mesh},
            timeout=60,
        )
        ok = bool(res.get("success"))
        job.log("ok" if ok else "error", f"save_dsg success={ok}")
        return res if ok else False

    job = svc.jobs.start("save_dsg", f"Save DSG on {robot}", run)
    return {"job_id": job.id}
