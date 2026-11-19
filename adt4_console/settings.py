"""Server configuration (built by ``cli.py`` or directly by tests)."""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field

STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"


@dataclass
class Settings:
    host: str = "0.0.0.0"
    port: int = 8090
    token: str | None = None
    topology: str | None = None  # None -> fleet_helpers default topology.yaml
    network: str | None = None  # None -> detect_network()
    output_root: str = "~/adt4_output"
    # mission-prep edit files the Launch window / map editor list (searched recursively);
    # None -> <output_root>/mission_prep
    prep_dir: str | None = None
    neo4j: dict[str, str] = field(default_factory=dict)  # name -> uri
    prior_neo4j: str | None = None
    # the target maps picked in the viewer are loaded into (map_loader); None: the first
    live_neo4j: str | None = None
    # the --neo4j target the SAM3 server reads its keyframes from: a live-map load
    # reloads SAM3 only when it loads into that target
    sam3_neo4j: str | None = None
    neo4j_user: str = field(
        default_factory=lambda: os.environ.get("HERACLES_NEO4J_USERNAME", "neo4j")
    )
    neo4j_password: str = field(
        default_factory=lambda: os.environ.get("HERACLES_NEO4J_PASSWORD", "")
    )
    maps: dict[str, str] = field(default_factory=dict)  # name -> run dir
    ros: bool = True
    fleetlink: bool = True
    zenoh_connect: str | None = None
    # live image-store mirror (fleetlink C6): None -> the current run's
    # $ADT4_OUTPUT_DIR/live (else <output_root>/console_<start>/live), "" = off
    live_store_root: str | None = None
    # live state for the SAM3 query server: None -> <output_root>/console_live.json,
    # "" = off (live_service.py)
    live_state_path: str | None = None
    robots: list[str] | None = None
    # the active launch (experiment, robots, prior map) survives console restarts
    # here; None = memory only (tests).  cli.py sets ~/.adt4_console/active_launch.json
    launch_state_path: str | None = None
    # the Launch form's last choices (prior map, sessions, robots, ...) per base
    # station; None = memory only (tests).  cli.py sets ~/.adt4_console/launch_memory.json
    launch_memory_path: str | None = None
    planner_ns: str | None = None  # omniplanner namespace (base station name)
    # trials (trials.py): the recorder helper on every machine (None: the
    # dcist_ws checkout's dcist_launch_system/bin/adt4-rec) and how often each
    # recording machine is asked for its status while a trial runs
    trial_recorder: str | None = field(
        default_factory=lambda: os.environ.get("ADT4_REC_CMD")
    )
    trial_poll_s: float = 10.0
    # how often a recording machine is asked again which of its topics have a
    # publisher (the stack may be restarted mid-trial); 0: only at the start
    trial_topic_recheck_s: float = 60.0
    # browser terminals on fleet machines (a shell for anyone who can reach the console)
    terminal: bool = False
    static_dir: pathlib.Path = STATIC_DIR

    # background work (tests switch these off)
    fleet_poll: bool = True
    status_poll_s: float = 10.0
    neo4j_poll_s: float = 2.0
    confirm_timeout_s: float = 300.0
    ros_domain_id: int | None = None  # None -> ROS_DOMAIN_ID env

    # chat
    llm: bool = True  # use an LLM when credentials are available
    # "auto": OpenAI if ADT4_OPENAI_API_KEY/OPENAI_API_KEY is set, else Anthropic
    llm_provider: str = "auto"
    llm_model: str | None = (
        None  # None -> provider default (gpt-4.1-mini / claude-opus-5-5)
    )
    # optional hard cap on cumulative chat spend (persisted); None: no cap
    llm_budget_usd: float | None = None
    llm_usage_path: str = "~/.adt4_console/llm_usage.json"

    # SAM3 query server (agentic_navigation sam3_frontend/query_server.py):
    # chat's search_robot_images tool + the /api/sam3 commit/discard routes
    sam3_url: str = field(
        default_factory=lambda: os.environ.get(
            "SAM3_QUERY_SERVER_URL", "http://127.0.0.1:8077"
        )
    )
    sam3_timeout_s: float = 180.0

    # geo + missions (API.md §14-16)
    geo_anchors: dict[str, str] = field(
        default_factory=dict
    )  # src id/name ("*": all) -> json
    tiles_dir: str = "~/.adt4_console/tiles"
    tiles_live: bool = (
        False  # fetch missing satellite tiles from the internet (and keep them)
    )
    missions_dir: str | None = (
        None  # None -> memory only (tests); cli: ~/.adt4_console/missions
    )
    quad_images_dir: str | None = (
        None  # where approved quad detections' images are kept
    )
    # packed mesh tiles + levels of detail cached per mesh file (mesh_lod.py);
    # None -> memory only (tests); cli: ~/.adt4_console/mesh_cache
    mesh_cache_dir: str | None = None
    # map editing (API.md §17)
    snapshots_dir: str | None = None  # None -> <tmp>; cli: ~/.adt4_console/snapshots
    # "auto": stage edits while a launch is active, apply immediately otherwise;
    # "staged" / "immediate" force one behaviour
    map_edit_mode: str = "auto"
    # Penn quad (API.md §16): JSON over ZMQ PUSH -> PULL, replies to a PULL we bind
    quad_cmd_uri: str | None = field(  # quad_api, e.g. tcp://10.10.10.107:6555
        default_factory=lambda: os.environ.get("ADT4_QUAD_CMD_URI") or None
    )
    quad_image_uri: str | None = field(  # camera_geo_api, e.g. tcp://10.10.10.107:5555
        default_factory=lambda: os.environ.get("ADT4_QUAD_IMAGE_URI") or None
    )
    # our address the quad connects back to (None: the local address on the route)
    quad_reply_host: str | None = field(
        default_factory=lambda: os.environ.get("ADT4_QUAD_REPLY_HOST") or None
    )
    # takeoff point (lat, lon); None: calibrated from the quad's GPS on first contact
    quad_takeoff: tuple[float, float] | None = None
    quad_utm_origin: tuple[float, float] = (0.0, 0.0)  # UtmToMapInfo.local_utm_origin
    quad_map_offset: tuple[float, float] = (0.0, 0.0)  # UtmToMapInfo.map_offset
    quad_hfov_deg: float = 70.0  # camera horizontal field of view (projection only)
    quad_arrive_m: float = 3.0  # arrival radius when no "arrived" event comes
    quad_goal_timeout_s: float = 600.0
    quad_poll_s: float = 1.0  # status poll (map glyph, arrival fallback)

    @property
    def output_root_path(self) -> pathlib.Path:
        return pathlib.Path(os.path.expanduser(self.output_root))
