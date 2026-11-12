import {afterEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {FleetPage, activeRecording} from "../src/pages/Fleet";
import {useFleet} from "../src/stores/fleet";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const json = (o: unknown, status = 200) => new Response(JSON.stringify(o), {status, headers: {"Content-Type": "application/json"}});
const flush = () => act(async () => void (await new Promise(r => setTimeout(r, 0))));

const MACHINES = {
  network: "mit_wifi",
  machines: [
    {name: "hamilton", role: "robot", ip: "198.51.100.1", user: "swarm", online: true, ping: 3, ssh: true},
    {name: "gauss", role: "robot", ip: "203.0.113.179", user: "swarm", online: true, ping: 3, ssh: true},
    {name: "willow", role: "base_station", ip: "203.0.113.5", user: "swarm", online: true, ping: 1, ssh: true},
  ],
};
const REC = {
  active: true,
  pane: "%3",
  target: "adt4_system:7.0",
  output: "/home/swarm/adt4_output/run1/recorded_data",
  started: Date.now() / 1000 - 120,
  size_bytes: 2_500_000_000,
  pid: 104,
  count: 1,
};
const IDLE = {active: false, pane: null, output: null, started: null, size_bytes: null};
const status = (rec: object) => ({
  updated: Date.now() / 1000,
  machines: {hamilton: {tmux_sessions: "adt4_system", recording: rec}, gauss: {tmux_sessions: "adt4_system", recording: IDLE}, willow: {tmux_sessions: "none"}},
});

const buttons = (text: string, root: ParentNode = document) => [...root.querySelectorAll<HTMLButtonElement>("button")].filter(b => b.textContent === text);
const row = (name: string) => document.querySelector(`[data-testid="machine-${name}"]`)!;
const posts = (f: ReturnType<typeof vi.fn>) => f.mock.calls.filter(c => (c[1] as RequestInit | undefined)?.method === "POST");

describe("Fleet: stop a robot's bag recording", () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    vi.unstubAllGlobals();
  });
  const mount = (st: object) => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "api/config") return json({terminal: false});
      if (init?.method === "POST") return json({ok: true, robot: "hamilton", job_id: "recording_stop-7-1"});
      return json({});
    });
    vi.stubGlobal("fetch", fetchMock);
    useFleet.setState({machines: MACHINES as never, status: st as never, nodes: {robots: {}} as never, loaded: true, error: null, load: async () => {}});
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root!.render(<FleetPage />));
    return fetchMock;
  };

  it("shows the REC badge and Stop bag only on the robot that is recording", async () => {
    mount(status(REC));
    await flush();
    expect(row("hamilton").textContent).toContain("● REC");
    expect(buttons("Stop bag", row("hamilton"))).toHaveLength(1);
    expect(row("hamilton").querySelector("[data-testid=rec-hamilton] .badge")!.getAttribute("title")).toContain(REC.output);
    for (const other of ["gauss", "willow"]) {
      expect(row(other).textContent).not.toContain("REC");
      expect(buttons("Stop bag", row(other))).toHaveLength(0);
    }
    // the recording ends: the badge and button go with the next poll
    act(() => useFleet.setState({status: status(IDLE) as never}));
    expect(buttons("Stop bag")).toHaveLength(0);
    expect(document.body.textContent).not.toContain("● REC");
  });

  it("confirms in the page naming the robot and bag, then starts a tracked job for that pane", async () => {
    const fetchMock = mount(status(REC));
    const {useJobs} = await import("../src/stores/jobs");
    await flush();
    act(() => buttons("Stop bag", row("hamilton"))[0].click());
    const modal = document.querySelector(".modal")!;
    expect(modal.textContent).toContain("hamilton");
    expect(modal.textContent).toContain(REC.output);
    expect(modal.textContent).toContain("adt4_system:7.0");
    expect(modal.textContent).toContain("2.5 GB");
    expect(posts(fetchMock)).toHaveLength(0);

    // cancel: nothing is sent
    act(() => buttons("Cancel", modal)[0].click());
    expect(document.querySelector(".modal")).toBeNull();
    expect(posts(fetchMock)).toHaveLength(0);

    act(() => buttons("Stop bag", row("hamilton"))[0].click());
    await act(async () => buttons("Stop bag", document.querySelector(".modal")!)[0].click());
    await flush();
    const [post] = posts(fetchMock);
    expect(post[0]).toBe("api/robots/hamilton/recording/stop");
    expect(JSON.parse(String((post[1] as RequestInit).body))).toEqual({pane: "%3"});
    expect(JSON.stringify(useJobs.getState())).toContain("recording_stop-7-1");
    expect(document.querySelector(".modal")).toBeNull();
  });

  it("cannot stop a recorder that is not in a tmux pane", async () => {
    mount(status({...REC, pane: null, target: null}));
    await flush();
    const b = buttons("Stop bag", row("hamilton"))[0];
    expect(b.disabled).toBe(true);
    expect(b.title).toContain("not in a tmux pane");
  });

  it("activeRecording: robots only, active only", () => {
    const robot = MACHINES.machines[0] as never;
    const base = MACHINES.machines[2] as never;
    expect(activeRecording(robot, {recording: REC as never})).toEqual(REC);
    expect(activeRecording(robot, {recording: IDLE as never})).toBeNull();
    expect(activeRecording(robot, {})).toBeNull();
    expect(activeRecording(base, {recording: REC as never})).toBeNull();
  });
});
