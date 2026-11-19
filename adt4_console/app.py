"""FastAPI application factory.

``create_app(settings, services=None)`` — tests inject a :class:`Services` built
with fakes (``build_services(settings, fleet_deps=..., launch_deps=..., receiver=...)``).
"""

from __future__ import annotations

import contextlib
import html
import logging
import os
import pathlib
import threading
from dataclasses import dataclass, field

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import __version__
from .auth import TokenAuthMiddleware
from .chat import ChatService
from .fleet_service import FleetDeps, FleetService
from .geo import GeoService
from .graph_service import GraphService, GraphUnavailable, NotFound
from .hub import Hub
from .jobs import JobManager
from .live_service import LiveService
from .manipulation_service import (
    ApprovalError,
    ManipulationApprovalService,
    ros_response_publisher,
)
from .optional import FeatureUnavailable
from .pick_result_service import PickResultService, ros_answer_publisher
from .ros_bridge import RosBridge, RosUnavailable
from .settings import Settings
from .teleop_service import TeleopService

log = logging.getLogger(__name__)

# NDJSON streams must flush chunk by chunk; GZipMiddleware would buffer them.
NO_GZIP_PATHS = ("/api/chat",)


class SelectiveGZipMiddleware:
    """GZip everything except streaming endpoints (same idea as agentic_navigation's chat_panel)."""

    def __init__(self, app, minimum_size=1024, exclude_paths=NO_GZIP_PATHS):
        self._plain_app = app
        self._gzip_app = GZipMiddleware(app, minimum_size=minimum_size)
        self._exclude = set(exclude_paths)

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") in self._exclude:
            await self._plain_app(scope, receive, send)
        else:
            await self._gzip_app(scope, receive, send)


def _fleet_helpers():
    from dcist_launch_system import fleet_helpers

    return fleet_helpers


@dataclass
class Services:
    settings: Settings
    hub: Hub
    jobs: JobManager
    fleet: FleetService
    graph: GraphService
    ros: RosBridge
    live: LiveService
    chat: ChatService
    launch_deps: object = None  # fleet.launch_workflow.LaunchDeps (None → real)
    helpers: object = field(default_factory=_fleet_helpers)  # fleet_helpers (or a fake)
    launch_state: object = None  # launch_state.LaunchState
    launch_memory: object = None  # launch_state.LaunchMemory (the form's last choices)
    teleop: TeleopService | None = None
    geo: GeoService | None = None
    missions: object = None  # mission.service.MissionService
    quad: object = None  # quad.link.QuadLink
    edits: object = None  # edit_service.EditService
    maps: object = None  # map_loader.MapLoader: the live map and switching it
    scene_repair: object = (
        None  # scene_repair.SceneRepair: scene change -> replan (API.md §20)
    )
    manipulation: ManipulationApprovalService | None = None
    # pick_result_service.PickResultService: "did the robot pick it up?"
    pick_result: PickResultService | None = None
    planner_state: object = (
        None  # planner_state.PlannerState: what the planner believes
    )
    trials: object = None  # trials.TrialService: fleet-wide recordings, flags
    nav_viz: object = (
        None  # nav_viz.NavViz: MPPI / mid-range markers, GPS fix (on demand)
    )
    # set by wire_goal_arrival: stops the goal arrival-by-position thread
    goal_arrival_stop: object = None

    def start(self) -> None:
        if self.settings.fleet_poll:
            self.fleet.start()
        robots = self.settings.robots or self.fleet.robots()
        self.ros.start(robots)
        if self.teleop is not None:
            self.teleop.start()
        self.live.start()
        if self.quad is not None:
            self.quad.start()
        self.graph.start()
        self.graph.warm()

    def stop(self) -> None:
        if self.goal_arrival_stop is not None:
            self.goal_arrival_stop.set()
        if self.trials is not None:
            self.trials.shutdown()  # only the polling: recordings keep going
        for svc in (
            self.nav_viz,
            self.missions,
            self.quad,
            self.teleop,
            self.live,
            self.ros,
            self.graph,
            self.fleet,
        ):
            if svc is None:
                continue
            try:
                svc.stop()
            except Exception:  # noqa: BLE001
                log.exception("stopping %s", type(svc).__name__)


def build_services(
    settings: Settings,
    *,
    fleet_deps: FleetDeps | None = None,
    launch_deps=None,
    receiver=None,
    helpers=None,
    tile_fetch=None,
) -> Services:
    hub = Hub()
    jobs = JobManager(hub, confirm_timeout=settings.confirm_timeout_s)
    fleet = FleetService(settings, hub, fleet_deps)
    live = LiveService(settings, hub, receiver)
    graph = GraphService(settings, hub, live)
    live.graph = graph
    ros = RosBridge(settings, hub, fleet)
    chat = ChatService(settings, graph, live, ros)
    svc = Services(settings, hub, jobs, fleet, graph, ros, live, chat, launch_deps)
    # late-bound so tests can swap svc.ros.publish_now
    svc.teleop = TeleopService(
        lambda topic, t, fields: svc.ros.publish_now(topic, t, fields),
        hub,
        platform_of=lambda robot: svc.ros.platform(robot),
    )
    ros.teleop_latch = svc.teleop.stopped
    svc.geo = GeoService(settings, graph, fetch=tile_fetch)
    from .map_loader import MapLoader

    svc.maps = MapLoader(settings, graph, jobs, hub, svc.geo)
    from .mission.service import MissionService
    from .quad.link import QuadLink
    from .quad.protocol import QuadFrame

    svc.quad = QuadLink(
        settings.quad_cmd_uri,
        settings.quad_image_uri,
        QuadFrame(svc.geo.default_anchor() if settings.quad_cmd_uri or settings.quad_image_uri else None,
                  settings.quad_utm_origin, settings.quad_map_offset),
    )  # fmt: skip
    from .live_anchor import LiveAnchor
    from .nav_viz import FixToMap, NavViz

    svc.geo.live = LiveAnchor(hub)
    svc.nav_viz = NavViz(
        lambda topic, typ, cb: svc.ros.nav_subscribe(topic, typ, cb),
        lambda sub: svc.ros.nav_unsubscribe(sub),
        platform_of=lambda robot: svc.ros.platform(robot),
        hub=hub,
        to_map=FixToMap(svc.geo, live=svc.geo.live),
        pose_of=lambda robot: relocalized_xy(svc, robot),
        live_anchor=svc.geo.live,
    )
    svc.missions = MissionService(settings, hub, graph, svc.geo, live, ros, svc.quad)
    chat.missions = svc.missions
    # answers go out through the bridge; the bridge hands requests to the service
    svc.manipulation = ManipulationApprovalService(
        ros_response_publisher(ros),
        hub,
        available=lambda: svc.ros.available,
        error=lambda: svc.ros.error,
    )
    ros.manipulation = svc.manipulation
    svc.pick_result = PickResultService(
        ros_answer_publisher(ros),
        hub,
        available=lambda: svc.ros.available,
        error=lambda: svc.ros.error,
    )
    ros.pick_result = svc.pick_result
    from .planner_state import PlannerState

    svc.planner_state = PlannerState(settings, ros, svc.maps, fleet, hub)
    ros.planner_state = svc.planner_state
    if helpers is not None:
        svc.helpers = helpers
    from . import launch_state

    svc.launch_state = launch_state.LaunchState(settings.launch_state_path)
    launch_state.restore(svc)
    svc.launch_memory = launch_state.LaunchMemory(settings.launch_memory_path)
    from .edit_service import EditService

    svc.edits = EditService(settings, graph, hub, jobs,
                            launch_state=lambda: svc.launch_state.get() if svc.launch_state else None)  # fmt: skip
    chat.edits = svc.edits
    from .mission_prep.service import PrepService

    svc.prep = PrepService(svc.maps, svc.edits, graph, hub, svc.launch_memory)
    from .sget import SgetService

    # plan repair's scene edits (heracles_agents SGET), used while its writer runs
    chat.sget = SgetService(graph, ros, settings)
    svc.missions.edits = svc.edits
    wire_scene_repair(svc)
    wire_goal_arrival(svc)
    from .trials import TrialService

    svc.trials = TrialService(settings, fleet, lambda: svc.helpers, hub)
    svc.trials.context = lambda: trial_context(svc)
    svc.prep.trials = svc.trials
    svc.trials.jobs = jobs  # to log the question a job confirmation answers
    try:
        svc.trials.restore()  # a trial still recording when the console last exited
    except Exception:  # noqa: BLE001
        log.exception("restoring the active trial")
    return svc


def trial_context(svc) -> dict:
    """What the console is working with, for a trial's record: the active
    launch (experiment, robots, prior map), the live map with the hash of its
    scene graph file, and the missions.  Each part fails on its own."""
    import hashlib

    out: dict = {}

    def put(key, fn):
        try:
            out[key] = fn()
        except Exception as e:  # noqa: BLE001
            out[key] = {"error": f"{type(e).__name__}: {e}"}

    def live_map():
        st = svc.maps.status()
        keep = {
            k: st.get(k)
            for k in ("target", "uri", "run", "name", "dsg", "nodes", "ready")
        }
        dsg = keep.get("dsg")
        if dsg and os.path.isfile(str(dsg)) and os.path.getsize(str(dsg)) < 4 * 10**9:
            h = hashlib.sha256()
            with open(str(dsg), "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            keep["dsg_sha256"] = h.hexdigest()
        return keep

    put("launch", lambda: svc.launch_state.get() if svc.launch_state else None)
    put("map", live_map)
    put("missions", lambda: svc.missions.list()[:30] if svc.missions else [])
    put("ros", lambda: bool(svc.settings.ros))
    # mission-prep edit files on the map (each copied into the recording trial)
    put(
        "prep", lambda: svc.prep.trial_context() if getattr(svc, "prep", None) else None
    )
    return out


GOAL_ARRIVAL_PERIOD_S = 1.0


def check_goal_arrival(svc) -> list[str]:
    """Feed the goal tracker the relocalized position of each robot whose
    navigation-only plan can only be seen to finish by where the robot is (no
    per-action results: a Husky). Returns the robots checked."""
    ros = getattr(svc, "ros", None)
    if ros is None:
        return []
    goals = ros.goals
    robots = goals.wants_pose()
    for robot in robots:
        try:
            goals.on_pose(robot, relocalized_xy(svc, robot))
        except Exception:  # noqa: BLE001 — one robot's pose must not stop the loop
            pass
    return robots


def wire_goal_arrival(svc, period_s: float = GOAL_ARRIVAL_PERIOD_S) -> None:
    """A daemon thread that checks goal arrival by position every ``period_s``."""
    stop = threading.Event()
    svc.goal_arrival_stop = stop

    def loop():
        while not stop.wait(period_s):
            try:
                check_goal_arrival(svc)
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=loop, name="goal-arrival", daemon=True).start()


def _live_target(svc):
    try:
        return svc.maps.target()
    except Exception:  # noqa: BLE001 — no Neo4j target
        return None


def _object_row(svc, sym: str) -> dict | None:
    """The live map's Object ``sym`` ({c: class, x, y}) or None."""
    t = _live_target(svc)
    if t is None:
        return None
    from .graph_service import parse_symbol

    try:
        c, i = parse_symbol(sym)
        rows = t.query("MATCH (n:Object) WHERE n.nodeSymbol IN $syms "
                       "RETURN n.class AS c, n.center.x AS x, n.center.y AS y LIMIT 1",
                       syms=[f"{c.upper()}{i}", f"{c.upper()}({i})"])  # fmt: skip
    except Exception:  # noqa: BLE001
        return None
    return dict(rows[0]) if rows else None


def _region_row(svc, sym: str, xy=None) -> dict | None:
    """The live map's region (Room) holding Object ``sym`` ({symbol, name}): by
    the place that contains it or, not linked yet, the place nearest ``xy``."""
    t = _live_target(svc)
    if t is None:
        return None
    from .graph_service import parse_symbol

    rows = []
    try:
        c, i = parse_symbol(sym)
        rows = t.query("MATCH (r:Room)-[:CONTAINS]->(:MeshPlace)-[:CONTAINS]->(o:Object) "
                       "WHERE o.nodeSymbol IN $syms RETURN r.nodeSymbol AS symbol, r.name AS name LIMIT 1",
                       syms=[f"{c.upper()}{i}", f"{c.upper()}({i})"])  # fmt: skip
    except Exception:  # noqa: BLE001
        rows = []
    if not rows and xy is not None:
        try:
            rows = t.query("MATCH (r:Room)-[:CONTAINS]->(p:MeshPlace) WHERE p.center IS NOT NULL "
                           "WITH r, (p.center.x - $x) ^ 2 + (p.center.y - $y) ^ 2 AS d "
                           "RETURN r.nodeSymbol AS symbol, r.name AS name ORDER BY d LIMIT 1",
                           x=float(xy[0]), y=float(xy[1]))  # fmt: skip
        except Exception:  # noqa: BLE001
            rows = []
    return dict(rows[0]) if rows else None


def relocalized_xy(svc, robot: str):
    """The robot's fleetlink live pose in the global map frame, only when it is
    relocalized (``frame_align.ok``) or already reports the global frame; else
    None (a GPS fix must not be paired with an odom-frame pose)."""
    from .scene_repair import pose_in_global

    st = svc.live.state(robot) if svc.live is not None else None
    if not st or not st.get("pose"):
        return None
    fa = st.get("frame_align")
    if fa is None:
        rx = getattr(svc.live, "receiver", None)
        try:
            fa = (rx.link(robot) or {}).get("frame_align") if rx is not None else None
        except Exception:  # noqa: BLE001
            fa = None
    frame = st["pose"].get("frame") or ""
    parent = (fa or {}).get("parent") or "map"
    if not (fa or {}).get("ok") and frame != parent:
        return None
    return pose_in_global(st["pose"], fa)


def wire_scene_repair(svc) -> None:
    """Scene changes -> replan recommendations (scene_repair), and console edits ->
    SceneChangeMsg on /scene_graph/changes_applied."""
    from .scene_repair import SceneRepair, pose_in_global, scene_changes_from_edit

    def robot_xy(robot):
        """The robot's (x, y) in the planner's map frame: heracles' state updater
        writes it on the live map's Robot node; else the fleetlink live pose,
        aligned to the global frame (frame_align); else unknown."""
        t = _live_target(svc)
        if t is not None:
            try:
                rows = t.query("MATCH (r:Robot {name: $n}) RETURN r.position.x AS x, r.position.y AS y LIMIT 1",
                               n=robot)  # fmt: skip
                if rows and rows[0].get("x") is not None:
                    return (float(rows[0]["x"]), float(rows[0]["y"]))
            except Exception:  # noqa: BLE001 — no Robot node / Neo4j down
                pass
        st = svc.live.state(robot) if svc.live is not None else None
        if not st:
            return None
        fa = st.get("frame_align")
        if fa is None:
            rx = getattr(svc.live, "receiver", None)
            try:
                fa = (
                    (rx.link(robot) or {}).get("frame_align")
                    if rx is not None
                    else None
                )
            except Exception:  # noqa: BLE001
                fa = None
        return pose_in_global(st.get("pose"), fa)

    def position_of(sym):
        r = _object_row(svc, sym)
        return (r["x"], r["y"]) if r and r.get("x") is not None else None

    def map_changed(map_version, changes):
        """A scene change batch from ROS (change detection, plan repair, a chat
        edit made elsewhere): fold the nodes it names into the loaded map; one
        that names none (a connection edit) or cannot be folded is reloaded and
        viewers get the difference (GraphService.apply_change / refresh)."""
        t = _live_target(svc)
        if t is None:
            return
        syms = [c["symbol"] for c in changes or () if c.get("symbol")]
        if map_version is not None:  # nothing named: still a no-op if already seen
            svc.graph.apply_change(t.id, "scene changes", map_version, syms)
        else:
            svc.graph.refresh(t.id, "scene changes")

    def send(robot, pddl, constraints, engine, request=None):
        return svc.ros.send_pddl_goal(
            robot, pddl, constraints=constraints, engine=engine, request=request
        )

    def recheck(robot, goal, report):
        """The chat's LLM redoes the operator's request on the changed map."""
        chat = getattr(svc, "chat", None)
        return chat.recheck(robot, goal, report) if chat is not None else None

    def resume(robot):
        from .routes import teleop as teleop_routes  # Resume also clears a STOP latch

        return teleop_routes.resume(svc, svc.ros, robot)

    sr = SceneRepair(
        svc.hub, send=send, resume=resume, active_goals=lambda: svc.ros.active_goals(),
        plan_of=lambda robot: svc.ros.plan(robot),
        class_of=lambda sym: (_object_row(svc, sym) or {}).get("c"),
        position_of=position_of, robot_xy=robot_xy, map_changed=map_changed,
        recheck=recheck,
        recheck_on=lambda: getattr(svc, "chat", None) is not None and svc.chat.recheck_available(),
        decide=lambda qid, decision: svc.ros.decide_replan(qid, decision),
        platform_of=lambda robot: svc.ros.platform(robot),
        region_of=lambda sym, xy: _region_row(svc, sym, xy),
    )  # fmt: skip
    svc.scene_repair = sr
    svc.ros.scene_repair = sr
    held = getattr(svc.ros, "replan_pending", None)
    if held is not None:  # latched before scene repair was wired
        sr.on_replan_pending(held)

    def on_commit(src_id, out, prov):
        t = _live_target(svc)
        if t is not None and t.id != src_id:
            return  # an edit of another map: no robot plans on it
        source = f"console/{prov.get('operator') or 'operator'}"
        changes = scene_changes_from_edit(out.get("changes"))
        summary = out.get("summary")
        if isinstance(summary, (list, tuple)):
            summary = "; ".join(str(x) for x in summary)
        sr.on_applied(
            source, out.get("map_version"), changes, summary=summary or None, local=True
        )
        if changes and svc.ros.available:
            import threading

            def go():
                try:
                    svc.ros.publish_scene_changes(
                        source, out.get("map_version"), changes
                    )
                except Exception:  # noqa: BLE001
                    log.exception("publishing scene changes of an edit")

            threading.Thread(target=go, name="edit-scene-changes", daemon=True).start()

    svc.edits.on_commit = on_commit

    def on_stage(src_id, count, summary):
        t = _live_target(svc)
        if t is not None and t.id != src_id:
            return
        sr.on_staged(src_id, count, summary)

    svc.edits.on_stage = on_stage


PLACEHOLDER = """<!doctype html><html><head><meta charset="utf-8"><title>ADT4 Console</title>
<style>body{font-family:system-ui,sans-serif;background:#111;color:#ddd;margin:3rem}
code{color:#8cf}</style></head><body><h1>ADT4 Console</h1>
<p>The web UI is not built: there is no <code>index.html</code> in <code>{static_dir}</code>.</p>
<p>Build it (Node &ge; 20.19) and rebuild the package:
<code>cd adt4_console/frontend &amp;&amp; npm ci &amp;&amp; npm run build</code>, then
<code>colcon build --packages-select adt4_console</code>
(in Awesome-DCIST-T4, <code>install/python_setup.bash</code> does the first step).</p>
<p>The API is up: try <a href="/api/health">/api/health</a> (add <code>?token=…</code> if a token is set).</p>
</body></html>"""


def create_app(settings: Settings, services: Services | None = None) -> FastAPI:
    services = services or build_services(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        services.start()
        try:
            yield
        finally:
            services.stop()

    app = FastAPI(title="ADT4 Console", version=__version__, lifespan=lifespan)
    app.state.services = services

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, exc: NotFound):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(GraphUnavailable)
    async def _graph_unavailable(request: Request, exc: GraphUnavailable):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(RosUnavailable)
    async def _ros_unavailable(request: Request, exc: RosUnavailable):
        return JSONResponse({"detail": f"ROS unavailable: {exc}"}, status_code=503)

    @app.exception_handler(FeatureUnavailable)
    async def _feature_unavailable(request: Request, exc: FeatureUnavailable):
        return JSONResponse(
            {"detail": exc.detail, "feature": exc.feature}, status_code=503
        )

    @app.exception_handler(ImportError)
    async def _package_missing(request: Request, exc: ImportError):
        # a route's optional package (optional.FEATURES) is not in this workspace
        log.warning("%s %s: %s", request.method, request.url.path, exc)
        name = getattr(exc, "name", None) or str(exc)
        return JSONResponse(
            {"detail": f"not available in this workspace (needs {name})"},
            status_code=503,
        )

    @app.exception_handler(ApprovalError)
    async def _approval_error(request: Request, exc: ApprovalError):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    from .routes import (
        change_detection,
        chat,
        drives,
        edit,
        fleet,
        geo,
        graph,
        jobs,
        launch,
        live,
        manipulation,
        maps,
        mission,
        network,
        pick_result,
        plan,
        planner_state,
        prep,
        quad,
        sam3,
        scene_repair,
        teleop,
        terminal,
        trials,
        ws,
    )

    for mod in (fleet, launch, jobs, network, maps, graph, plan, live, chat, sam3, teleop, terminal,
                geo, mission, quad, edit, change_detection, scene_repair, manipulation, pick_result,
                planner_state, drives, trials, prep, ws):  # fmt: skip
        app.include_router(mod.router)
    app.include_router(live.robots_router)

    _mount_static(app, pathlib.Path(settings.static_dir))

    from .trials import TrialRequestLog

    app.add_middleware(TrialRequestLog)  # operator actions -> the trial's events.jsonl
    app.add_middleware(SelectiveGZipMiddleware, minimum_size=1024)
    app.add_middleware(TokenAuthMiddleware, token=settings.token)
    return app


def _mount_static(app: FastAPI, static_dir: pathlib.Path) -> None:
    """Serve the built SPA with an index.html fallback for client-side routes."""
    root = static_dir.resolve()
    placeholder = PLACEHOLDER.replace("{static_dir}", html.escape(str(root)))
    if not (root / "index.html").is_file():
        log.warning(
            "web UI not built (no %s): serving a placeholder page; run "
            "`npm ci && npm run build` in frontend/ and rebuild the package",
            root / "index.html",
        )

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path == "api" or path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        index = root / "index.html"
        if path:
            target = (root / path).resolve()
            if target.is_file() and root in target.parents:
                headers = (
                    {"Cache-Control": "public, max-age=31536000, immutable"}
                    if path.startswith("assets/")
                    else {}
                )
                # not in every Python's mimetypes table; browsers ignore a
                # manifest served as octet-stream (robot models: viewer/RobotModel)
                media = {
                    ".webmanifest": "application/manifest+json",
                    ".glb": "model/gltf-binary",
                }.get(target.suffix)
                return FileResponse(target, headers=headers, media_type=media)
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return HTMLResponse(placeholder)
