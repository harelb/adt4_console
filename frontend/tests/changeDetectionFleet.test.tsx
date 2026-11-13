import {afterEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {FleetChangeDetectionControl, fleetResultText, fleetSummary, type FleetCmdResult, type FleetState} from "../src/viewer/ChangeDetectionFleet";
import {AwcdChangesPanel} from "../src/viewer/AwcdChanges";
import {useAwcd} from "../src/stores/awcd";
import {ws} from "../src/ws";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const OK = {ok: true, reason: "relocalized (3 prior-map matches)"};
const NOT = {ok: false, reason: "not relocalized: no prior-map match yet"};
const FLEET: FleetState = {
  available: true,
  robots: [
    {robot: "euclid", state: "paused", reachable: true, reason: null, relocalized: OK},
    {robot: "gauss", state: null, reachable: false, reason: "offline (fleetlink link down)", relocalized: NOT},
    {robot: "hamilton", state: "paused", reachable: true, reason: null, relocalized: OK},
    {robot: "hilbert", state: "paused", reachable: true, reason: null, relocalized: NOT},
    {robot: "lewis", state: "running", reachable: true, reason: null, relocalized: OK},
  ],
};
const STARTED: FleetCmdResult = {
  ok: true,
  cmd: "start",
  robots: [
    {robot: "euclid", ok: true, skipped: false, message: "started", state: "running"},
    {robot: "gauss", ok: false, skipped: true, error: "offline (fleetlink link down)", state: null},
    {robot: "hamilton", ok: true, skipped: false, message: "started", state: "running"},
    {robot: "hilbert", ok: false, skipped: true, not_relocalized: true, error: "not relocalized: no prior-map match yet", state: "paused"},
    {robot: "lewis", ok: false, skipped: true, error: "already running", state: "running"},
  ],
  counts: {done: 2, skipped: 3, failed: 0},
};

describe("fleet change detection start / stop", () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  const posts: {url: string; body: unknown}[] = [];
  const gets: string[] = [];
  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    vi.unstubAllGlobals();
    posts.length = 0;
    gets.length = 0;
    useAwcd.setState({state: null, focus: null});
  });
  const stub = (fleet: unknown, result: (body: {override?: boolean}) => unknown) =>
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        posts.push({url: String(url), body});
        return new Response(JSON.stringify(result(body)));
      }
      gets.push(String(url));
      return new Response(JSON.stringify(fleet));
    }));
  const mount = async (node: React.ReactNode) => {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root!.render(node));
    await act(async () => await new Promise(r => setTimeout(r, 0)));
    return host;
  };
  const q = (el: HTMLElement, id: string) => el.querySelector(`[data-testid=${id}]`) as HTMLElement;
  const click = async (el: HTMLElement) => act(async () => {
    el.click();
    await new Promise(r => setTimeout(r, 0));
  });

  it("summarises the fleet from the robots' states", async () => {
    stub(FLEET, () => STARTED);
    const el = await mount(<FleetChangeDetectionControl />);
    expect(gets).toEqual(["api/change_detection/fleet"]);
    expect(q(el, "cd-fleet-summary").textContent).toBe("1 running, 2 paused, 1 not relocalized, 1 unreachable");
    expect(fleetSummary([])).toBe("no robots");
    expect(fleetSummary([{robot: "x", state: null, reachable: true, reason: null}])).toBe("1 not reported");
  });

  it("Start all calls the fleet route once and names who was skipped and why", async () => {
    stub(FLEET, () => STARTED);
    const el = await mount(<FleetChangeDetectionControl />);
    expect(q(el, "cd-start-anyway")).toBeNull(); // only after a skip
    await click(q(el, "cd-start-all"));
    expect(posts).toEqual([{url: "api/change_detection/start_all", body: {}}]);
    const res = q(el, "cd-fleet-result").textContent!;
    expect(res).toContain("started euclid, hamilton");
    expect(res).toContain("hilbert (not relocalized: no prior-map match yet)");
    expect(res).toContain("gauss (offline (fleetlink link down))");
    expect(res).toContain("lewis (already running)");
    // one row for the robots it was done for, then one per robot with its reason
    expect(Array.from(q(el, "cd-fleet-result").children).map(c => c.textContent)).toEqual([
      "started euclid, hamilton",
      "skipped gauss (offline (fleetlink link down))",
      "skipped hilbert (not relocalized: no prior-map match yet)",
      "skipped lewis (already running)",
    ]);
    expect(gets.filter(u => u === "api/change_detection/fleet")).toHaveLength(2); // refreshed
  });

  it("'Start anyway' appears only after a not-relocalized skip, asks, and overrides only those robots", async () => {
    stub(FLEET, body => (body.override ? {ok: true, cmd: "start", robots: [{robot: "hilbert", ok: true, skipped: false, message: "started", state: "running"}], counts: {done: 1, skipped: 0, failed: 0}} : STARTED));
    const el = await mount(<FleetChangeDetectionControl />);
    await click(q(el, "cd-start-all"));
    await click(q(el, "cd-start-anyway"));
    expect(posts).toHaveLength(1); // asking sends nothing
    expect(el.textContent).toContain("placed in the wrong frame");
    await click(q(el, "cd-start-anyway-go"));
    expect(posts[1]).toEqual({url: "api/change_detection/start_all", body: {robots: ["hilbert"], override: true}});
    expect(q(el, "cd-fleet-result").textContent).toContain("started hilbert");
    expect(q(el, "cd-start-anyway")).toBeNull();
  });

  it("Stop all calls the fleet route; a failed robot is named", async () => {
    const stopped: FleetCmdResult = {
      ok: false,
      cmd: "stop",
      robots: [
        {robot: "euclid", ok: true, skipped: false, message: "stopped", state: "paused"},
        {robot: "hamilton", ok: false, skipped: false, error: "timed out", state: "running"},
      ],
      counts: {done: 1, skipped: 0, failed: 1},
    };
    stub(FLEET, () => stopped);
    const el = await mount(<FleetChangeDetectionControl />);
    await click(q(el, "cd-stop-all"));
    expect(posts).toEqual([{url: "api/change_detection/stop_all", body: {}}]);
    const res = q(el, "cd-fleet-result");
    expect(res.textContent).toContain("stopped euclid");
    expect(res.textContent).toContain("FAILED hamilton (timed out)");
    expect(q(el, "cd-start-anyway")).toBeNull();
    expect(fleetResultText({ok: true, cmd: "stop", robots: [], counts: {done: 0, skipped: 0, failed: 0}})).toBe("no robots");
  });

  it("the buttons are disabled while a command is in flight", async () => {
    let release: (r: Response) => void = () => undefined;
    vi.stubGlobal("fetch", vi.fn((_url: string, init?: RequestInit) =>
      init?.method === "POST" ? new Promise<Response>(r => (release = r)) : Promise.resolve(new Response(JSON.stringify(FLEET)))));
    const el = await mount(<FleetChangeDetectionControl />);
    await click(q(el, "cd-start-all"));
    expect((q(el, "cd-start-all") as HTMLButtonElement).disabled).toBe(true);
    expect((q(el, "cd-stop-all") as HTMLButtonElement).disabled).toBe(true);
    await act(async () => {
      release(new Response(JSON.stringify(STARTED)));
      await new Promise(r => setTimeout(r, 0));
    });
    expect((q(el, "cd-start-all") as HTMLButtonElement).disabled).toBe(false);
  });

  it("a robot's live state updates the summary", async () => {
    stub(FLEET, () => STARTED);
    const el = await mount(<FleetChangeDetectionControl />);
    act(() => ws.inject({topic: "robot/euclid/change_detection", data: {robot: "euclid", state: "running", t: 2}} as never));
    expect(q(el, "cd-fleet-summary").textContent).toBe("2 running, 1 paused, 1 not relocalized, 1 unreachable");
  });

  it("shows nothing when ROS is off", async () => {
    stub({available: false, error: "ROS is off", robots: []}, () => STARTED);
    const el = await mount(<FleetChangeDetectionControl />);
    expect(q(el, "cd-fleet")).toBeNull();
  });

  it("the AWCD panel has the fleet control next to the fleet reset", async () => {
    const awcd = {available: true, topic: "/willow/awcd_base_station/reconciled_changes", t: 1, frame_id: "map", changes: [], counts: {added: 0, removed: 0}, robots: {}};
    vi.stubGlobal("fetch", vi.fn(async (url: string) => new Response(JSON.stringify(String(url).endsWith("change_detection/fleet") ? FLEET : awcd))));
    const el = await mount(<AwcdChangesPanel />);
    expect(q(el, "cd-start-all")).not.toBeNull();
    expect(q(el, "cd-stop-all")).not.toBeNull();
    expect(q(el, "cd-reset-all")).not.toBeNull();
  });
});
