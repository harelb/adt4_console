// The viewer's load bar showed "Mesh preparing" for good, and the live-map panel
// "loading the scene graph 0%" with nothing loading: a reload of a source whose
// mesh is already on screen started a "preparing" task that nothing ended (a
// robot's live mesh reports only its first load), hiding a source left its tasks
// behind, and an empty Neo4j (every stage "pending", no job) read as a load.
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import {readFileSync} from "node:fs";
import {join} from "node:path";
import {beginTask, endTask, loadTaskIds, resetLoading, useLoading} from "../src/stores/loading";
import {mapLoading, type LiveMap, type MapStage} from "../src/stores/liveMap";
import {useViewer} from "../src/stores/viewer";

const FIX = join(__dirname, "fixtures");
const ID = "live:hamilton";
const MANIFEST = {tile_size: 8, enc: 2, tiles: {"0_0_0": {hash: "h", nv: 3, nf: 1, origin: [0, 0, 0], lod: [[10, "e"]]}}};

function respond(url: string): Response {
  if (url.endsWith("/snapshot")) return new Response(readFileSync(join(FIX, "graph_small.msgpack")), {status: 200});
  if (url.endsWith("/mesh/manifest")) return new Response(JSON.stringify(MANIFEST), {status: 200, headers: {"Content-Type": "application/json"}});
  return new Response(JSON.stringify({}), {status: 200, headers: {"Content-Type": "application/json"}});
}

describe("viewer load bar", () => {
  beforeEach(() => {
    resetLoading();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (u: RequestInfo | URL) => respond(String(u)));
  });
  afterEach(() => {
    vi.restoreAllMocks();
    resetLoading();
  });

  it("a reload of a source whose mesh is on screen starts no 'preparing' task", async () => {
    useViewer.setState({
      order: [ID],
      sources: {[ID]: {id: ID, kind: "live", enabled: true, showMesh: true, status: "ready", manifest: MANIFEST, manifestRev: 1, version: 7}},
    } as never); // prettier-ignore
    const seen: string[][] = [];
    const unsub = useLoading.subscribe(() => seen.push(loadTaskIds()));
    await useViewer.getState().reloadSource(ID);
    unsub();
    expect(seen.flat()).not.toContain(`mesh:${ID}`);
  });

  it("the first load of a source still shows 'Mesh preparing' until the manifest is in", async () => {
    useViewer.setState({
      order: [ID],
      sources: {[ID]: {id: ID, kind: "live", enabled: true, showMesh: true, status: "idle", manifestRev: 0}},
    } as never); // prettier-ignore
    const p = useViewer.getState().reloadSource(ID);
    expect(loadTaskIds()).toContain(`mesh:${ID}`);
    expect(useLoading.getState().summary.parts.some(x => x.text.includes("preparing"))).toBe(true);
    await p;
  });

  it("hiding a source drops its graph, mesh and trajectory tasks", () => {
    useViewer.setState({order: [ID], sources: {[ID]: {id: ID, kind: "live", enabled: true, status: "loading"}}} as never);
    beginTask(`graph:${ID}`, "Graph");
    beginTask(`mesh:${ID}`, "Mesh", "bytes", null, "preparing");
    beginTask(`traj:${ID}`, "");
    useViewer.getState().enableSource(ID, false);
    expect(loadTaskIds()).toEqual([]);
  });

  it("ending a task twice does not restart the finished batch", () => {
    beginTask("mesh:a", "Mesh", "bytes", 10);
    endTask("mesh:a");
    const at = useLoading.getState().finishedAt;
    expect(at).not.toBeNull();
    endTask("mesh:a");
    expect(useLoading.getState().finishedAt).toBe(at);
  });
});

const stage = (id: string, state: MapStage["state"], pct = 0): MapStage => ({id, label: id, state, pct, detail: null});
const live = (over: Partial<LiveMap>): LiveMap => ({
  target: "live", run: null, name: null, has_images: false, has_features: false, ready: false, job_id: null,
  stages: [stage("graph", "pending"), stage("images", "pending")], ...over,
}); // prettier-ignore

describe("mapLoading", () => {
  it("an empty Neo4j (every stage pending, no job) is not loading", () => {
    expect(mapLoading(live({}))).toBe(false);
  });
  it("a load job, or a stage with progress, is loading", () => {
    expect(mapLoading(live({job_id: "j1", run: "/r", name: "r"}))).toBe(true);
    expect(mapLoading(live({stages: [stage("graph", "running", 30), stage("images", "pending")]}))).toBe(true);
  });
  it("a finished map is not loading", () => {
    expect(mapLoading(live({ready: true, stages: [stage("graph", "done", 100), stage("images", "skipped", 100)]}))).toBe(false);
  });
});
