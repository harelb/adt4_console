import {afterEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {FleetPage} from "../src/pages/Fleet";
import {useFleet} from "../src/stores/fleet";
import {terminalUrl} from "../src/components/Terminal";
import {shortBattery, shortDisk, shortGpu, shortLoad} from "../src/lib/fleetFormat";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

// jsdom has no canvas: a stand-in xterm for the terminal modal
vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    cols = 80;
    rows = 24;
    loadAddon() {}
    open() {}
    focus() {}
    write() {}
    dispose() {}
    onData() {
      return {dispose() {}};
    }
    onResize() {
      return {dispose() {}};
    }
  },
}));
vi.mock("@xterm/addon-fit", () => ({FitAddon: class {fit() {}}}));
vi.mock("@xterm/xterm/css/xterm.css", () => ({}));

const json = (o: unknown, status = 200) => new Response(JSON.stringify(o), {status, headers: {"Content-Type": "application/json"}});
const flush = () => act(async () => void (await new Promise(r => setTimeout(r, 0))));

const MACHINES = {
  network: "mit_wifi",
  machines: [
    {name: "hamilton", role: "robot", ip: "198.51.100.1", user: "swarm", online: true, ping: 3, ssh: true},
    {name: "gauss", role: "robot", ip: "203.0.113.179", user: "swarm", online: false, ping: false, ssh: false},
  ],
};
const STATUS = {updated: Date.now() / 1000, machines: {hamilton: {tmux_sessions: "adt4_system, nstf_build"}}};

const buttons = (title: string) => [...document.querySelectorAll<HTMLButtonElement>("button")].filter(b => (b.title || b.textContent || "").includes(title));

describe("Fleet tmux sessions", () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    vi.unstubAllGlobals();
  });
  const mount = (terminal: boolean) => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "api/config") return json({terminal});
      if (init?.method === "POST") return json({ok: true});
      return json({});
    });
    vi.stubGlobal("fetch", fetchMock);
    useFleet.setState({machines: MACHINES, status: STATUS as never, nodes: {robots: {}} as never, loaded: true, error: null, load: async () => {}});
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root!.render(<FleetPage />));
    return fetchMock;
  };

  it("kills a session after confirming, and warns about unsaved maps for adt4_system", async () => {
    const fetchMock = mount(false);
    await flush();
    expect(buttons("kill tmux session adt4_system")).toHaveLength(1);
    // terminals are off: no Terminal or attach buttons
    expect(buttons("Terminal")).toHaveLength(0);
    expect(buttons("attach to")).toHaveLength(0);

    act(() => buttons("kill tmux session adt4_system")[0].click());
    expect(document.body.textContent).toContain("tmux kill-session -t adt4_system");
    expect(document.body.textContent).toContain("Stop & Save first");
    expect(fetchMock.mock.calls.some(c => (c[1] as RequestInit | undefined)?.method === "POST")).toBe(false);

    await act(async () => buttons("Kill session")[0].click());
    await flush();
    const post = fetchMock.mock.calls.find(c => (c[1] as RequestInit | undefined)?.method === "POST")!;
    expect(post[0]).toBe("api/fleet/hamilton/tmux/adt4_system/kill");
  });

  it("stop (Ctrl-C, then kill) runs as a tracked job, without the adt4_system warning for other sessions", async () => {
    const fetchMock = mount(false);
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "api/config") return json({terminal: false});
      if (init?.method === "POST") return json({ok: true, job_id: "j42"});
      return json({});
    });
    const {useJobs} = await import("../src/stores/jobs");
    await flush();
    act(() => buttons("stop nstf_build")[0].click());
    expect(document.body.textContent).not.toContain("Stop & Save first");
    expect(document.body.textContent).toContain("then kills the session");
    await act(async () => buttons("Stop session")[0].click());
    await flush();
    expect(fetchMock.mock.calls.find(c => (c[1] as RequestInit | undefined)?.method === "POST")![0]).toBe("api/fleet/hamilton/tmux/nstf_build/stop");
    expect(JSON.stringify(useJobs.getState())).toContain("j42");
  });

  it("offers terminals only on reachable machines when enabled", async () => {
    mount(true);
    await flush();
    // hamilton only: gauss is offline
    expect(buttons("shell on")).toHaveLength(1);
    expect(buttons("attach to")).toHaveLength(2);
  });

  it("shows a disabled open button per session while terminals are off", async () => {
    mount(false);
    await flush();
    const open = [...document.querySelectorAll<HTMLButtonElement>("button[aria-label^='open ']")];
    expect(open.map(b => b.getAttribute("aria-label"))).toEqual(["open adt4_system on hamilton", "open nstf_build on hamilton"]);
    expect(open.every(b => b.disabled)).toBe(true);
    expect(open[0].title).toContain("terminals are off");
  });

  it("opens a tmux session in a read-write terminal with a live-session warning", async () => {
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        disconnect() {}
      },
    );
    mount(true);
    await flush();
    const open = document.querySelector<HTMLButtonElement>("button[aria-label='open adt4_system on hamilton']")!;
    expect(open.disabled).toBe(false);
    act(() => open.click());
    const modal = document.querySelector(".modal")!;
    expect(modal.textContent).toContain("tmux");
    expect(modal.querySelector("[role=alert]")!.textContent).toContain("every keystroke goes to adt4_system on hamilton");
  });

  it("fits the machines table: compact columns, a Stop & Save button in every robot row", async () => {
    const st = {
      updated: Date.now() / 1000,
      machines: {
        hamilton: {tmux_sessions: "adt4_system", load: "0.52/0.40/0.33", mem: "3.2Gi/15Gi", gpu: "12% 1200/8000M", disk: "120G / 500G (76% used)", battery: "99% (Not charging)", radio: "N/A"},
      },
    };
    mount(false);
    useFleet.setState({status: st as never});
    await flush();
    const heads = [...document.querySelectorAll(".fleet-table thead th")].map(t => t.textContent);
    expect(heads).toEqual(["machine", "link", "tmux", "resources", "disk free", "battery", "radio", ""]);
    for (const r of MACHINES.machines.filter(m => m.role === "robot")) {
      const row = document.querySelector(`[data-testid="machine-${r.name}"]`)!;
      const stop = [...row.querySelectorAll<HTMLButtonElement>(".cell-actions button")].filter(b => b.textContent === "Stop & Save");
      expect(stop).toHaveLength(1);
      expect(stop[0].className).toContain("btn-xs");
    }
    const row = document.querySelector('[data-testid="machine-hamilton"]')!;
    const battery = row.querySelector('td[data-label="battery"]')!;
    expect(battery.textContent).toBe("99%");
    expect(battery.querySelector("[title]")!.getAttribute("title")).toBe("99% (Not charging)");
    expect(row.querySelector('td[data-label="disk free"]')!.textContent).toBe("120G");
    expect(row.querySelector('td[data-label="resources"]')!.textContent).toBe("load 0.52mem 3.2Gi/15Gigpu 12%");
  });

  it("formats status strings compactly", () => {
    expect(shortBattery("40% (Charging)")).toBe("40% ⚡");
    expect(shortBattery("87.6%")).toBe("88%");
    expect(shortBattery("N/A")).toBeNull();
    expect(shortDisk("120G / 500G (76% used)")).toBe("120G");
    expect(shortLoad("1.5/1.2/0.9")).toBe("1.5");
    expect(shortGpu("12% 1200/8000M")).toBe("12%");
  });

  it("builds the terminal websocket URL", () => {
    const u = new URL(terminalUrl("hamilton", "adt4_system", 120, 40));
    expect(u.protocol).toBe("ws:");
    expect(u.pathname.endsWith("/api/fleet/hamilton/terminal")).toBe(true);
    expect(Object.fromEntries(u.searchParams)).toMatchObject({cols: "120", rows: "40", session: "adt4_system"});
    expect(new URL(terminalUrl("hamilton", null, 80, 24)).searchParams.has("session")).toBe(false);
  });

  it("Stop & Save tears the stack down unless unchecked", async () => {
    const fetchMock = mount(false);
    await flush();
    const posts = () => fetchMock.mock.calls.filter(c => (c[1] as RequestInit | undefined)?.method === "POST");
    act(() => buttons("Stop & Save")[0].click());
    expect(document.body.textContent).toContain("shut down the rest of the stack");
    await act(async () => buttons("Stop & Save").at(-1)!.click());
    await flush();
    expect(posts()[0][0]).toBe("api/robots/hamilton/stop_save");
    expect(JSON.parse(String((posts()[0][1] as RequestInit).body))).toEqual({teardown: true});

    act(() => buttons("Stop & Save")[0].click());
    act(() => (document.querySelector(".modal input[type=checkbox]") as HTMLInputElement).click());
    await act(async () => buttons("Stop & Save").at(-1)!.click());
    await flush();
    expect(JSON.parse(String((posts()[1][1] as RequestInit).body))).toEqual({teardown: false});
  });
});
