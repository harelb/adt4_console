# ADT4 console

A web console for the base station of an
[Awesome-DCIST-T4](https://github.com/MIT-SPARK/Awesome-DCIST-T4) (ADT4) robot fleet:
fleet status, launching, map transfers, the 3D scene-graph viewer, planning, missions,
chat and the live robot link. It also replaces RViz on the base station: the viewer shows
the scene graph, mesh, occupancy grid, executor paths, camera frames and plans.

* Server: FastAPI (`adt4_console/`), a ROS 2 Jazzy `ament_python` package.
* UI: React + react-three-fiber single-page app (`frontend/`), built at install time into
  `adt4_console/static/` and shipped with the package.
* Live robot data: [fleetlink](https://github.com/harelb/fleetlink) (optional).
* API and wire format: [`docs/API.md`](docs/API.md).

## Install

In an ADT4 workspace the console is the `adt4_console` submodule: `install/python_setup.bash`
installs the Python packages into `spark_env` and builds the UI, then `colcon build` installs it.
By hand:

```bash
# 1. the browser UI (Node >= 20.19); writes adt4_console/static/
cd adt4_console/frontend && npm ci && npm run build && cd ..
# 2. the server's Python packages, into the environment the console runs in
pip install -e ".[server,llm]"        # or ADT4's install/spark_requirements.txt
# 3. the ROS package (the UI bundle is installed as package data)
colcon build --packages-select adt4_console
```

Without step 1 the server still runs: `/` shows a page that says how to build the UI, and the
API works.

ROS dependencies are in `package.xml` (`dcist_launch_system` for the fleet topology, `heracles`
for Neo4j maps, the message packages the planner bridge uses). Some features need packages a
workspace may not have; without them the feature is off, the UI hides it and its routes answer
503 (`GET /api/health` lists `features`):

| feature | needs |
|---|---|
| launching, map transfers, preflight, network tools | `dcist_launch_system.fleet` (fleet core) |
| live robot link (graph, video, lidar, image mirror) | `fleetlink` |
| keyframe images, object crops | `hydra_python.image_store` (Hydra's image store) |
| map editing | `heracles.graph_edit` |
| plan repair's scene edits in the chat | `heracles_agents.scene_graph_edit` |
| in-console PDDL planning / GRSTAPS-X | `omniplanner` (`dsg_pddl`, `omniplanner.grstaps_planner`) |
| executor action results, Spot lease state | `robot_executor_msgs` `ActionResultMsg`, `RuntimeGuardsMsg` |

Take lease is offered for a Spot whose executor serves `~/take_lease`.

## Run

```bash
source /opt/ros/jazzy/setup.bash && source <ws>/install/setup.bash   # ROS is optional (--no-ros)
ros2 run adt4_console adt4-console \
    --map prior=<run dir> \
    --neo4j live=bolt://localhost:7687
```

`python -m adt4_console` is the same. When the starting Python lacks the server's packages
(`ros2 run` uses the system Python), the command re-executes itself with
`$ADT4_ENV/spark_env/bin/python3`. In ADT4 the base-station sessions start it (launch
components `console` / `console_neo4j`). Open `http://<base station>:8090/`.

| flag | meaning |
|---|---|
| `--host`, `--port` | listen address (default `0.0.0.0`, port `$ADT4_CONSOLE_PORT` or 8090); refuses a busy port |
| `--token TOKEN` | require `Authorization: Bearer` or `?token=` on `/api` (default `$ADT4_CONSOLE_TOKEN`) |
| `--map NAME=RUN_DIR` | file graph source (a run dir with `hydra/backend/dsg.json`), repeatable |
| `--neo4j NAME=URI`, `--prior-neo4j`, `--live-neo4j`, `--sam3-neo4j` | Neo4j graph sources and their roles |
| `--topology PATH`, `--network NET` | fleet topology (default: `dcist_launch_system`'s) and active network (default: detected) |
| `--output-root DIR` | where experiment outputs live (default `~/adt4_output`) |
| `--zenoh-connect EP` | zenoh router for fleetlink |
| `--robots a,b`, `--planner-ns NS` | robots to plan for (default: the topology's), omniplanner's namespace |
| `--no-ros`, `--no-fleetlink`, `--no-llm` | turn subsystems off |
| `--llm-provider`, `--llm-model`, `--llm-budget-usd` | chat LLM; hard spend cap, kept in `~/.adt4_console/llm_usage.json` |
| `--sam3-url URL` | SAM3 query server for robot-image search |
| `--terminal` | browser terminals (ssh) on fleet machines; pair with `--token` |
| `--geo-anchor`, `--tiles-dir`, `--tiles-live` | map-to-UTM anchors and satellite tiles (`scripts/geo/`) |
| `--map-edits auto\|staged\|immediate` | when map edits are staged |

With a token, open the console once as `http://host:8090/?token=TOKEN`; the UI keeps it for the
tab and removes it from the address bar.

### Environment variables

| variable | used for |
|---|---|
| `ADT4_CONSOLE_PORT` | default `--port` |
| `ADT4_CONSOLE_TOKEN` | default `--token` |
| `ADT4_CONSOLE_TERMINAL` | default `--terminal` (`1` / `true`) |
| `HERACLES_NEO4J_USERNAME`, `HERACLES_NEO4J_PASSWORD` | Neo4j credentials for every `--neo4j` source (ADT4 sets them from `ADT4_NEO4J_USERNAME/PASSWORD`) |
| `ADT4_OPENAI_API_KEY`, `HERACLES_OPENAI_API_KEY`, `OPENAI_API_KEY` | chat with OpenAI (first one set) |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` | chat with Anthropic |
| `SAM3_QUERY_SERVER_URL` | default `--sam3-url` |
| `ADT4_AUTO_TAKE_LEASE` | sending a plan takes the named Spots' lease (default on; `0` turns it off) |
| `ADT4_ENV` | where `spark_env` is (re-exec, remote image-store commands) |
| `ADT4_ROBOT_NAME` | default `--planner-ns` |
| `ADT4_OUTPUT_DIR` | where GRSTAPS-X writes its problem files (default under `~/adt4_output`) |
| `ADT4_OMNIPLANNER_PLUGINS` | omniplanner plugin file the console's planner preview reads |
| `config` | ADT4 config name, to find that config's omniplanner plugins |
| `ADT4_FLEET_LINK`, `ADT4_FLEETLINK_PORT` | fleetlink mode and router port (default `--zenoh-connect`) |
| `ADT4_GUROBI_LICENSE` / `GRB_LICENSE_FILE`, `ADT4_ITAGS_BINARY`, `ADT4_GRSTAPS_TIMEOUT_S`, `ADT4_GRSTAPS_WORK_DIR`, `ADT4_GRSTAPS_ALLOW_HUSKY` | GRSTAPS-X mission planning |
| `ROS_DOMAIN_ID` | ROS domain of the planner bridge |

API keys are read from the environment only.

### Developing the UI

```bash
cd frontend
npm ci
CONSOLE_BACKEND=http://127.0.0.1:8090 npm run dev   # Vite dev server, proxies /api
npm test                                            # vitest
npm run build                                       # writes ../adt4_console/static
```

`npm run mock` runs an offline mock backend; `npm run test:browser` is a Playwright smoke test
that loads every page and saves screenshots (`CONSOLE_URL=... SHOT_DIR=...`).

## Pages

The header shows the server, WebSocket ("live"), ROS and fleetlink status, plus the jobs drawer.

* **Fleet.** Machines from the topology with ping/ssh/online state, remote status (tmux
  sessions, load) and per-robot node health from `ros_system_monitor`, with a readiness light.
  *Stop & save* calls the robot's `/shutdown` service, falling back to tmux keys. Each tmux
  session has *stop* (a job: Ctrl-C every pane, wait up to 15 s for the nodes to exit, then
  kill the session) and *kill* (kill the session at once).
* **Launch.** Choose robots, session, experiment name, mapping or relocalize mode, a prior map,
  recording, and an optional base-station session. *Dry run* lists the exact commands.
  *Launch* runs the same workflow as the `fleet-adt4` TUI as a job.
* **Maps.** Experiment outputs on every machine. Actions: *transfer* (rsync to this machine),
  *verify* (hash comparison across machines), *load into Neo4j* (ingest), and *open in viewer*.
* **Robot viewer** (`#/viewer`, formerly "Map viewer"). The 3D scene graph and mesh for any number of
  sources at once (`file:` run dirs, `neo4j:` databases and `live:` robots), plus each robot's
  cameras, teleop, plans and link.
  * **Robot focus** (toolbar, next to *Layers*): *All robots* or one robot, each with its colour and
    online / stale / offline state; `[` / `]` cycle it, clicking a legend chip or a robot's camera
    tile focuses that robot. Remembered per browser (a robot the console no longer knows reads as
    all). Every robot-specific part follows it: **Camera** (that robot's cameras incl. the Spot
    all-cameras grid; all robots: one main camera per robot), **Teleop** (drives the focused
    robot only; with all robots it offers a button per robot, which focuses it; changing the focus
    while armed disarms first: zero velocity + release), **Plan** and **Chat** (the plan robot is
    the focused robot; all robots: the Plan tab's own pick), **Live link** (that robot only),
    graph statistics (other robots' `live:` sources hidden), **Images** (preselects that robot's
    newest live keyframe pool) and the 3D view (other robots' live map, paths, plan and glyph
    are dimmed; *Display → Other robots*: dim / hide / always full; *Follow* keeps the camera on
    the focused robot). Panels that follow show the robot as a chip (× = all robots).
  * **Drive layout** (toolbar *Drive layout*; on by default while a robot is focused, your choice is
    remembered): the map, the focused robot's camera and the Teleop tab at once, no floating
    windows. Wide screens pin the camera above the side-panel tabs (drag the edge between them;
    ⧉ pops it out, the tabs below default to Teleop); phones show the camera as a
    picture-in-picture under the toolbar with Teleop in the bottom sheet. Teleop keys still drive
    only while the Teleop panel has focus: clicking the map or the camera disarms it.
  Panels:
  * left: *Sources* (enable, mesh/graph toggles, tile progress), *Layers* and *Edges* toggles,
    *Live overlays* (below), *Display* (layer stacking, mesh opacity, bounding boxes, trajectory);
  * toolbar: robot focus, search (labels, names, symbols), follow a robot, drive layout, 3D/top camera;
  * right tabs: **Inspect** (selected node, attributes, images; with nothing selected,
    **graph statistics** for each enabled source: nodes per layer, edges per kind, mesh size and top
    object labels), **Mission** and **Map** (the mission planner's tabs, below; *Map* edits a Neo4j
    map), **Plan** (goto/TSP picking, PDDL, language goals, current plan and executor
    pause/resume), **Chat**, **Live link** (per-robot staleness, latency, budgets and auto mode;
    see below), **Teleop**, **Images** and **Camera**.
* **Mission planner** (`#/mission`). Multi-agent missions written in natural language, planned
  over the satellite view, simulated, then executed (API.md §14–16).
  * The left pane is the **Mission chat**. Describe the mission ("quad1 search A1 for a crate; when
    found spot1 inspects it, spot2 guards the gate"). The assistant looks things up with Cypher and
    proposes a mission card: *Simulate* or *Open in Mission tab*. Ask for changes to revise it.
    A proposed mission opens in the Mission tab at once (on a phone the chat stays up; the side
    panel opens on it). A card for an older revision says *Superseded by rN — open latest*, and
    its *Simulate* is off while that mission's simulation plays or a live run is going. The
    assistant's map lookups fold into one line per turn (*Looked up the map · 3 queries, 2
    retried*); inside, a failed query shows a one-line reason with the full Neo4j error below.
    It does not substitute silently: an area or class the map does not have gets a clarifying
    question ("There's no area called 'courtyard': draw it, or pick one of: …"), and whatever it
    still interpreted is listed in amber on the card (*Assumed*).
  * The **Mission** tab:
    * the mission picker (each with its date / time; missions with errors are marked "⚠ n
      errors"; names are unique) and planner choice (chat, or omniplanner for ground-only
      commands). *Update mission from roster* puts the roster's starts / robots / kinds, the
      no-go zones and the mock objects into the open mission (a new revision); it says what
      differs and is off when nothing does;
    * **⌖ Fit mission** (map toolbar, *Fit* in the tab, key **F**) frames the mission's agents,
      paths and areas clear of the toolbars; opening a mission and *Simulate* do it too;
    * the transport (simulate / pause / speed / reset / auto-approve / *Execute…* / *Abort*) and a
      per-agent timeline (click it to jump); when Simulate or Execute is off, the reason is
      written under it;
    * issues and the task list. A target the map no longer has (e.g. `O(17)` after a re-ingest)
      offers *Pick on map* (click an object, a place or a point: the new target), *Drop task* and
      *Re-plan in chat* (puts the mission and the problem in the chat box). A mission planned on
      another map version, or before versions were recorded, says it may be stale, with *Accept
      current map*;
    * **detections**: quad images with *Accept* (this run only), *Add to map* (Neo4j object +
      image, ground robots can go to it; still offered after *Accept*) and *Reject*. Click an
      image to open it, and drag a box in it to report something yourself. Detections waiting
      for review put a count on the Mission tab and a notice over the map ("quad1 found 'crate'
      — review"; *Review* scrolls to it);
    * the **agent roster** (up to 5; spot/husky/quad, *sim only* or bound to a real robot).
      Opening a mission puts its agents in the roster. Each agent's **start** can be moved:
      * type it: map *x / y* (metres, 2 decimals) or, when the map has a GPS anchor, *lat / lon*
        (the *map x/y | lat/lon* switch); Enter or leaving the fields sets it, Esc reverts, a
        field that is not a number is flagged and not sent. *Live pose* (bound robots only)
        clears the start: the robot starts where it is. ⌖ arms the click-on-map start tool;
      * or **drag the agent on the map** (mouse or finger): grabbing a glyph holds the camera for
        that gesture, a ghost ring shows the new start with its x / y and lat/lon, release sets
        it, Esc cancels. *Snap to places* (or Shift while dragging) lands it on the nearest place
        node within 4 m;
      * a moved start of an agent in the open mission is a new revision: the preview (paths,
        timeline, issues) is recomputed at once. Starts are locked while a live run is going or a
        simulation plays; moving one of a paused or finished simulation resets it.
        *↶ Undo: move spot1* puts the last move back. New agents start 5 m apart (at least 4 m
        from every other start);
      * a start outside the map's extent is flagged in the row (a warning, not a block); a ground
        start more than 4 m from every place gets the preview warning `start_offgraph` with
        *Snap to nearest place*;
    * **Areas**: one *Draw area* tool (click the corners; Enter or double-click finishes,
      Backspace drops the last corner, Esc cancels). The popover then asks the name, the
      purpose (*Search area* or *No-go zone*) and the scope: *This mission only* (the default)
      or *Save to map* (an area in the Neo4j map that every mission, the chat, Cypher and
      omniplanner see; no-go zones are mission only). The list shows both scopes (badge
      *Mission* / *Map*) with *Save to map* and *Remove*; the chat gets them as one list;
    * **Mock objects** for rehearsals (only the simulated quad camera sees them; a search finds
      one when its query matches the label, and the preview warns when none does);
    * the **satellite** controls and a lat/lon + UTM cursor readout.
  * The **Map** tab (API.md §17, Neo4j maps only) is the one map editor: toggle *edit map*, then
    map clicks and search hits select (Shift-click adds; *Lasso* selects inside a polygon by
    layer) without switching tabs. One node: class, name, position, mark a place blocked; several:
    class in one batch. *Remove* (retire, with an *Undo* toast), *Connect* / *Disconnect* two
    nodes, *Focus on its members* for an area (the rest of the map dims), *Advanced › Group
    selection into area*. *Add object*: type a class, click the map. *History* lists every edit
    (who, when, via ui/chat) with *Undo*; *Snapshots* keeps named copies of the map (download as
    zip, *Restore…* is a job with the typed confirm `restore`). While a launch is active the edits
    are **staged**: the bar shows the staged diff and *Publish to robots* needs the typed word
    `publish` (one unit of work, one map-version bump). The Inspector links here (*Edit in Map
    tab*). The chat proposes edits as cards with a diff (*Approve & apply* / *Reject*).
  * While editing the map, a red frame and strip say **Live map editing — changes write to
    Neo4j** (amber **Staged — publish to send** while edits are staged); Undo buttons name what
    they undo (*Undo: remove O(18)*).
  * Keys (also in the **?** card, *Mission / map edit*): Enter / double-click finish a drawing,
    Backspace drops a corner, Esc cancels, **Delete** removes the edit selection (undoable),
    **Ctrl/⌘+Z** undoes the newest of your last start move and your last map edit (this browser),
    **F** fits the mission, **Space** simulates / pauses / resumes. None fire while typing.
  * On `/mission` the side panel shows *Mission | Map | Inspect*; Plan, Teleop, Images, Live link
    and Camera are under *More* (phones keep the full strip). The map opens with the places
    layers off (satellite + objects); layer changes made there are remembered for it. Object
    labels are decluttered: the ones that fit without overlapping, the selected node's and those
    nearest the pointer first.
  * One map tool is armed at a time (a start, Draw area, mock objects, a new target, Lasso, Add
    object): arming one disarms the other, and one banner over the map says what the next click
    does. Area display: Layers › *Area boundaries* and *Colour members by area*.
  * The side nav folds to icons (the arrow at its foot; remembered per browser, folded by default
    up to 1366 px), and on a narrow map the toolbar keeps the essentials and puts the rest under ⋯.
  * The map opens in the top view over **satellite imagery** (an offline tile cache, see
    `scripts/geo/README.md`). Missions draw their planned legs (dashed = not started, bold =
    active), search areas, camera footprints, detections and agents. Inspect works as in the viewer.
  * Start it with the map's GPS anchor and the tile cache:
    ```bash
    adt4-console --neo4j penn=neo4j://localhost:7697 \
        --geo-anchor '*=~/data/pennovation_2025/courtyard-alleyways/output/geo_anchor.json' \
        [--tiles-live] [--quad-cmd-uri tcp://*:6555 --quad-image-uri tcp://<quad>:6556 \
        --quad-utm-origin E N --quad-map-offset X Y]
    # rehearsal quad (renders images from the tile cache):
    python -m adt4_console.quad.mock_server --anchor <geo_anchor.json> --plant crate:30,-80
    ```
  * End-to-end capture: `node frontend/scripts/capture-mission-planner.mjs` (see its header);
    moving starts (typed, dragged, touch-dragged on a Pixel 7):
    `node frontend/scripts/capture-start-relocation.mjs` (it revises the mission: use a scratch console);
    map editing: `node frontend/scripts/capture-map-edit.mjs` (it edits the map: use a scratch DB).
* **Network.** The zenoh router and its links (deploy configs, start/stop the local router),
  the Silvus radio RSSI matrix and routes, and an iperf throughput test.
* **Config.** Topology editor (machines, roles, addresses) and the console settings.

### RViz replacement

| RViz display | console |
|---|---|
| OccupancyMap (`hydra/tsdf/occupancy`) | *Live overlays → <robot>: occupancy*: toggle + opacity. A textured plane at the grid's origin; occupied cells dark red, unknown faint grey, free transparent |
| MLP Path / smooth path / MLP target | *Live overlays*: smooth path (cyan dashed), MLP path (amber), MLP target (pink marker) |
| Image (`<robot>_zed/rgb/image_rect_color`, `semantic_overlay/image_raw`) | **Camera** tab, *Still* mode: robot, camera and width; auto-refresh at 1 Hz by default (0.2–5 Hz) or manual; shows the frame's age, size in KB and round trip. *Video* mode: a live stream (below) |
| DsgStatsPanel | **Inspect** tab with nothing selected |
| robot pose, plan | robot glyphs (with a stale marker), plan overlay (current action highlighted) |

Camera frames are pulled **on demand**. The robot sends nothing until someone has the Camera tab
open. Each frame is resized and JPEG-encoded on the robot. A 640 px frame at quality 70 is
typically a few tens of kB, so expect about that many kB/s at 1 Hz while the tab is open.

**Live video** (*Camera → Video*) streams a camera as H.264 or VP8 instead. The robot encodes
with its system `ffmpeg`, the browser decodes with WebCodecs and draws to a canvas, so a
640 px stream costs about its bitrate: **150 / 300 / 800 kbit/s** for the *low / medium / high*
presets, about 19 / 38 / 100 kB/s. Pick 1–15 fps and 320–960 px (never above the camera's own
width). The codec is chosen automatically: h264 when the browser can decode it, else vp8, else
one JPEG per frame (which costs several times more for the same picture). The stream runs only
while the tab is visible: the browser sends a keepalive every 2 s, the console forwards one to
the robot every 2 s while any viewer is alive, and the robot stops 6 s after the last one. The
stats line shows the received fps and kbit/s, the decode time, the lag (browser clock minus
robot sample time, so it includes clock skew), skipped frames and "waiting for key frame". The
second line is the robot's own report (encoder, size, sent rate, frames it dropped for budget).

### Typed confirms

Destructive steps run as jobs and stop at a confirm dialog (the jobs drawer shows it as well).
Two of them make you **type a word** before *Yes* is enabled:

* `wipe`: *Maps → Load into Neo4j* replaces the target database. The dialog shows the current
  node count of the target.
* `confirm`: *Launch*, when the experiment output directory already exists on a robot and
  holds at least 1 GB. Smaller directories get a plain yes/no.
* `restore`: *Map › Snapshots › Restore…* replaces the map in Neo4j with the snapshot.
* `publish` (in the panel, not a job): publishing staged map edits while robots are live.
* `delete`: a permanent node delete (`DELETE /api/edit/{src}/nodes/{sym}?hard=true&confirm=delete`);
  the UI only retires (undoable).

Killing an existing `adt4_system` session before a launch is a plain yes/no. An unanswered confirm
times out after 300 s and counts as *No*.

## Live link and fleetlink budgets

Live data travels in six classes with separate byte budgets. All values are bytes per second.
They are set per robot in the agent's `fleetlink/config/fleetlink.yaml` or as ROS params, and
can be changed live in **Live link**.

| class | carries | default budget | on congestion |
|---|---|---|---|
| C0 control | requests/acks (resync, budget, tiles, resend, ping) | none | block (never dropped) |
| C1 state | pose, guards, health, executor results, executor paths | 5 kB/s | drop, latest value wins |
| C2 graph | scene-graph deltas, closest to the robot first | 50 kB/s | block (backpressure) |
| C3 mesh | mesh tiles + occupancy grid | 100 kB/s | drop; repaired via manifest / `resend` |
| C4 media | camera frames on request | 200 kB/s | request refused (`429`), never queued |
| C5 video | live camera video (all streams together) | 400 kB/s | delta frames dropped, then skipped until the next key frame; key frames borrow up to 1 s of budget |

The Live link tab shows each budget as a log slider (500 B/s to 2 MB/s, applied when you release
it), the measured rate as a sparkline, and the link's staleness, latency, drops, gaps and resyncs.
*Auto budget* lets the robot adapt C2/C3 to the measured goodput (AIMD). *Resync* re-streams the
whole graph. The sliders cover C1–C3. Change C4/C5 with
`POST /api/live/{r}/budget {"cls": "C5", "bps": ...}` or the agent params `budget_C4` / `budget_C5`.
Design details: [fleetlink](https://github.com/harelb/fleetlink).

## Troubleshooting

* **UI says "Backend unreachable".** The server is down or a proxy returned 502/504. The UI keeps
  the last data and retries every 5 s.
* **"Not authorised: open with ?token=".** Reopen the console as `/?token=...`.
* **"ROS not connected"** on planning or nodes. The console was started with `--no-ros`, or
  `rclpy` failed to import (source ROS before starting). `GET /api/health` shows `ros_error`.
* **No live robots / fleetlink red.** The console could not reach zenoh at `--zenoh-connect`. It
  keeps retrying every 10 s, so start `rmw_zenohd` and wait. `GET /api/health` shows `fleetlink_error`.
  Check that the robot's `fleetlink` window is running and that both sides share a router mesh.
  `ROS_DOMAIN_ID` does not matter to fleetlink, because it uses plain zenoh keys `adt4/<robot>/...`.
* **Live map stuck or partial.** See Live link: `gaps`/`resyncs` going up means C2 is losing
  messages, so raise the C2 budget or press *Resync*. A tile bar that stays below 100 % means C3
  is starved, so raise C3.
* **No occupancy grid** ("no grid received yet"). Hydra publishes `/<robot>/hydra/tsdf/occupancy`
  only when that output is enabled. Check with `ros2 topic hz /<robot>/hydra/tsdf/occupancy`. The
  grid is sent at most every `grid_period_s` (5 s), and only when it changed.
* **No paths.** Paths appear only while the Spot executor publishes them, and only changes are sent.
  Paths in a frame other than the scene graph's are transformed with TF when possible. Otherwise
  they are drawn as they are (the panel's frame tells you which).
* **Camera: "No reply (timeout)" (504).** The robot did not answer within 5 s: the agent is not
  running, the link is saturated, or the robot is unknown. **404 "no frame yet"**: nothing has
  been received on that camera's topic. Check the agent's `cameras` param, which takes
  `name=topic` relative to the robot namespace. **429**: the C4 budget is spent. Lower the
  refresh rate or the width, or raise `budget_C4`.
* **Frame age keeps growing.** The camera topic stopped. Age is measured on the robot from when
  the frame arrived, so it is valid under sim time.
* **Video: "Robot: ffmpeg not found ..." / "ffmpeg exited ...".** The robot has no usable
  `ffmpeg` (with libx264 / libvpx) and fell back to JPEG frames at the same fps. Install ffmpeg
  or point the agent's `ffmpeg` param (or `$FLEETLINK_FFMPEG`) at one.
* **Video: "waiting for key frame" for seconds.** Frames were lost (on the link, or for the C5
  budget, see *robot dropped*), and delta frames cannot be decoded without their predecessors.
  Key frames come every 2 s. Lower the bitrate or fps, or raise C5.
* **Video: 429 on start.** The parameters were changed again within 1 s. The UI retries by itself.
* **Viewer is slow.** Turn off sub-keyframes or edges, and hide the mesh of sources you do not need.

## Tests

```bash
python -m pytest -q tests          # with the workspace sourced; tests that need ROS 2,
                                   # Neo4j, spark_dsg or optional packages skip
cd frontend && npm ci && npm test && npm run build
```

CI (`.github/workflows/test.yml`) runs the pure-Python tests, the frontend tests and build,
and ruff. Optional inputs:

* `ADT4_CONSOLE_TEST_NEO4J` (default `neo4j://localhost:7697`): a Neo4j **test** instance.
  `tests/test_integration.py::test_ingest_into_neo4j_then_serve` **wipes it**.
* `ADT4_TEST_SCRATCH_NEO4J` (default `:7701`, skipped when nothing listens): a throwaway Neo4j
  the snapshot-restore and incremental-refresh tests wipe.
* `ADT4_CONSOLE_TEST_RUN`, `ADT4_CONSOLE_TEST_IMAGED_RUN`: real run directories for the
  integration tests.
* `ADT4_CONSOLE_TEST_HIDE=fleetlink,robot_executor_msgs.msg.RuntimeGuardsMsg,...`: run as if
  those packages were not installed.

## License

Apache-2.0 (`LICENSE`, `NOTICE`). The robot models in `frontend/public/models/` are baked by
`frontend/scripts/bake_robot_models.py` from the Spot (spot_tools_ros) and Clearpath Husky
A200 (clearpath_platform_description) URDF meshes and keep those meshes' terms, not
Apache-2.0: see [`frontend/public/models/README.md`](frontend/public/models/README.md).
