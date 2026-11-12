"""FastAPI application factory.

``create_app(settings, services=None)`` — tests inject a :class:`Services` built
with fakes (``build_services(settings, fleet_deps=..., launch_deps=..., receiver=...)``).
"""

from __future__ import annotations

import contextlib
import html
import logging
import pathlib
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
    logbook: object = None  # logbook.Logbook: sessions, relocalization gate, missions
    hub_tap: object = None  # eventtap.HubTap
    collector: object = None  # mission_log.collector.Collector

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
        if self.logbook is not None:
            self.logbook.start(_active_experiment(self))
            self.logbook.publish_mission(force=True)  # the ticker retries until ROS is up
            if self.collector is not None and self.settings.mission_log:
                # missions an earlier console left pending / deferred: keep retrying
                self.collector.watch(pathlib.Path(self.logbook.base_dir()).glob("missions/*"))

    def stop(self) -> None:
        for svc in (
            self.collector,
            self.logbook,
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
    svc.missions = MissionService(settings, hub, graph, svc.geo, live, ros, svc.quad)
    chat.missions = svc.missions
    # answers go out through the bridge; the bridge hands requests to the service
    svc.manipulation = ManipulationApprovalService(
        ros_response_publisher(ros),
        hub,
        available=lambda: svc.ros.available,
        error=lambda: svc.ros.error,
        default_robot=lambda: (settings.robots or fleet.robots() or ["euclid"])[0],
    )
    ros.manipulation = svc.manipulation
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
    from .sget import SgetService

    # plan repair's scene edits (heracles_agents SGET), used while its writer runs
    chat.sget = SgetService(graph, ros, settings)
    svc.missions.edits = svc.edits
    wire_scene_repair(svc)
    wire_logbook(svc)
    return svc


def _active_experiment(svc) -> str | None:
    try:
        st = svc.launch_state.get() if svc.launch_state is not None else None
    except Exception:  # noqa: BLE001
        return None
    return (st or {}).get("experiment") if isinstance(st, dict) else None


def wire_logbook(svc) -> None:
    """Mission logging (eventlog): the logbook (sessions / reloc gate / missions)
    and the hub tap.  Files only with ``settings.mission_log``."""
    from . import eventlog
    from .eventtap import HubTap
    from .logbook import Logbook

    settings = svc.settings
    if settings.mission_log:
        eventlog.enable(True)

    def image_lookup(sym):
        """The image files the goal card shows for ``sym`` (the live map's node images)."""
        t = _live_target(svc)
        if t is None:
            return []
        d = t.node_detail(sym)
        return [t.image(d["sym"], i).path for i in range(len(d.get("images") or []))]

    lb = Logbook(settings, svc.hub, live_state=lambda r: svc.live.state(r) if svc.live is not None else None,
                 image_lookup=image_lookup, persist=bool(settings.mission_log),
                 state_path=settings.logbook_state_path)  # fmt: skip

    def ros_publish(topic, msg_type, fields):
        if not svc.ros.available:
            raise RuntimeError("ROS is not up")
        svc.ros.publish_latched(topic, msg_type, fields)

    lb.ros_publish = ros_publish
    svc.logbook = lb
    svc.hub_tap = HubTap()
    svc.hub.taps.append(svc.hub_tap)
    lb.on_mission_edge = lambda why: [svc.hub_tap.clock_offset(r, why=why) for r in list(svc.hub_tap.link)]
    lb.run_ids_fn = lambda: session_run_ids(svc)
    svc.collector = make_collector(svc)
    lb.on_mission_end = lambda m, mdir: on_mission_end(svc, m, mdir)


def session_run_ids(svc) -> dict:
    """{machine: [run_id]} of the open session: the base experiment's runs of this
    session (run_info.json) and the run each robot's fleetlink agent reports."""
    import pathlib

    from .mission_log import read_json

    lb = svc.logbook
    sid = ((lb.session or {}) if lb is not None else {}).get("session_id")
    out: dict = {}
    if lb is not None and sid:
        base = []
        for p in sorted(pathlib.Path(lb.base_dir()).glob("runs/*/run_info.json")):
            info = read_json(p) or {}
            if info.get("session_id") == sid:
                base.append(info.get("run_id") or p.parent.name)
        if base:
            out["base"] = base
    for robot, run in dict(getattr(svc.hub_tap, "runs", {}) or {}).items():
        if run.get("run_id") and (not sid or not run.get("session_id") or run.get("session_id") == sid):
            out[robot] = [run["run_id"]]
    return out


def _executing_robots(svc) -> list[str]:
    """Robots executing a goal now (the collector waits for them)."""
    try:
        goals = svc.ros.goals.recent(50) if svc.ros is not None else []
    except Exception:  # noqa: BLE001
        return []
    busy = set()
    for g in goals:
        for r, v in (g.get("robots") or {}).items():
            if v.get("stage") == "executing":
                busy.add(r)
    return sorted(busy)


def _scene_run_read(svc):
    """Read-only Cypher on the base map (the planner's Neo4j), or None."""
    from . import cypher_tools

    def run_read(q):
        src = None
        want = f"neo4j:{svc.settings.live_neo4j}" if svc.settings.live_neo4j else None
        for s in svc.graph.list() if svc.graph is not None else []:
            if s.get("kind") == "neo4j" and (want is None or s.get("id") == want):
                src = svc.graph.get(s["id"])
                break
        if src is None:
            return {"error": "no Neo4j map is connected"}
        return cypher_tools.run_read(src, q, max_rows=20000)

    return run_read


def make_collector(svc):
    from . import eventlog
    from .mission_log.collector import Collector

    s = svc.settings

    def endpoint(name):
        from .routes.maps import _endpoint

        return _endpoint(svc, name)

    def on_change(mid, st):
        try:
            svc.hub.publish("missions/collect", {"mission_id": mid, "state": st.get("state"),
                                                 "machines": {k: v.get("state") for k, v in st["machines"].items()}})  # fmt: skip
        except Exception:  # noqa: BLE001
            pass

    helpers = svc.helpers
    return Collector(
        ssh=getattr(helpers, "ssh_cmd", None), rsync=getattr(helpers, "rsync_transfer", None), endpoint=endpoint,
        output_root=s.output_root, executing=lambda: _executing_robots(svc), run_read=_scene_run_read(svc),
        live_root=eventlog.root, settle_s=s.collect_settle_s, bwlimit_kbps=s.collect_bwlimit_kbps,
        on_change=on_change,
    )  # fmt: skip


def on_mission_end(svc, mission: dict, mdir) -> None:
    """After mission.end: the base DSG snapshot (dsg_saver -> dsg_end.json), then
    the collect job (unless --no-mission-collect)."""
    from . import eventlog

    ns = svc.settings.planner_ns
    if ns and svc.ros is not None and getattr(svc.ros, "available", False):
        path = str(pathlib.Path(mdir) / "dsg_end.json")
        try:
            res = svc.ros.call_service(f"/{ns}/dsg_saver/save_dsg", "dcist_launch_system_msgs/srv/SaveDsg",
                                       {"save_path": path, "include_mesh": False}, timeout=60)  # fmt: skip
            ok = bool((res or {}).get("success"))
        except Exception as e:  # noqa: BLE001 — no dsg_saver in this session
            ok, res = False, {"error": str(e)}
        eventlog.emit("console", "mission.dsg_saved", {"mission_id": mission.get("mission_id"), "ok": ok,
                                                        "path": path, "error": (res or {}).get("error")})  # fmt: skip
    if svc.settings.mission_collect and svc.collector is not None and svc.settings.mission_log:
        svc.collector.collect(mdir, auto=True)


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

    def invalidate():
        t = _live_target(svc)
        if t is None:
            return
        t.invalidate()
        svc.graph.notify_version(t.id, "scene changes")

    def send(robot, pddl, constraints, engine):
        return svc.ros.send_pddl_goal(
            robot, pddl, constraints=constraints, engine=engine
        )

    def resume(robot):
        from .routes import teleop as teleop_routes  # Resume also clears a STOP latch

        return teleop_routes.resume(svc, svc.ros, robot)

    sr = SceneRepair(
        svc.hub, send=send, resume=resume, active_goals=lambda: svc.ros.active_goals(),
        plan_of=lambda robot: svc.ros.plan(robot),
        class_of=lambda sym: (_object_row(svc, sym) or {}).get("c"),
        position_of=position_of, robot_xy=robot_xy, invalidate=invalidate,
    )  # fmt: skip
    svc.scene_repair = sr
    svc.ros.scene_repair = sr

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
        mission_records,
        network,
        plan,
        quad,
        sam3,
        scene_repair,
        teleop,
        terminal,
        ws,
    )
    from .routes import logbook as logbook_routes

    for mod in (fleet, launch, jobs, network, maps, graph, plan, live, chat, sam3, teleop, terminal,
                geo, mission, quad, edit, change_detection, scene_repair, manipulation, logbook_routes,
                mission_records, ws):  # fmt: skip
        app.include_router(mod.router)
    app.include_router(live.robots_router)

    _mount_static(app, pathlib.Path(settings.static_dir))

    from .eventtap import RestTap

    app.add_middleware(RestTap)  # innermost: sees plain bodies (operator actions -> events)
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
