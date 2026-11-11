import {afterEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {MemoryRouter} from "react-router";
import {LaunchPage} from "../src/pages/Launch";
import {useFleet} from "../src/stores/fleet";
import {defaultExperiment, lastPriorValue, nextExperimentName} from "../src/lib/launchMemory";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const json = (o: unknown, status = 200) => new Response(JSON.stringify(o), {status, headers: {"Content-Type": "application/json"}});
const flush = () => act(async () => void (await new Promise(r => setTimeout(r, 0))));

describe("nextExperimentName", () => {
  const now = new Date(2026, 10, 11, 14, 2);
  it("bumps a _vN suffix, or adds _v2", () => {
    expect(nextExperimentName("courtyard_loop", "relocalize", now)).toBe("courtyard_loop_v2");
    expect(nextExperimentName("courtyard_loop_v2", "relocalize", now)).toBe("courtyard_loop_v3");
    expect(nextExperimentName("run_v9", "mapping", now)).toBe("run_v10");
  });
  it("gives the form's timestamped default a fresh timestamp", () => {
    expect(nextExperimentName("reloc_20261111_0930", "relocalize", now)).toBe("reloc_20261111_1402");
    expect(nextExperimentName(null, "mapping", now)).toBe(defaultExperiment("mapping", now));
  });
  it("builds the prior select value, falling back to the base station", () => {
    expect(lastPriorValue({prior_path: "/out/m1", prior_source: "hamilton"}, "sequoia")).toBe("hamilton\u0000/out/m1");
    expect(lastPriorValue({prior_path: "/out/m1", prior_source: null}, "sequoia")).toBe("sequoia\u0000/out/m1");
    expect(lastPriorValue({prior_path: ""}, "sequoia")).toBe("");
  });
});

const MACHINES = {
  network: "mit_wifi",
  machines: [
    {name: "hamilton", role: "robot", ip: "198.51.100.1", user: "swarm", online: true, ping: 3, ssh: true},
    {name: "gauss", role: "robot", ip: "203.0.113.179", user: "swarm", online: true, ping: 3, ssh: true},
    {name: "sequoia", role: "base_station", ip: "198.51.100.9", user: "swarm", online: true, ping: 1, ssh: true, local: true},
  ],
};
const MAPS = {
  base: "sequoia",
  machines: {
    sequoia: [
      {name: "courtyard_fused", path: "/home/swarm/adt4_output/courtyard_fused", size: "2G", is_prior: true},
      {name: "other_map", path: "/home/swarm/adt4_output/other_map", size: "1G"},
    ],
  },
};
const LAST = {
  robots: ["gauss"],
  session: "spot-reloc",
  experiment: "courtyard_reloc",
  mode: "relocalize",
  prior_path: "/home/swarm/adt4_output/courtyard_fused",
  prior_source: "sequoia",
  record: true,
  base_station: "sequoia",
  bs_session: "base_station_fleetlink",
  link_network: null,
  link_mode: "fleetlink",
  saved_at: Date.now() / 1000 - 600,
};

describe("Launch form remembers the last launch", () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    vi.unstubAllGlobals();
  });

  const mount = (last: unknown, path = "/launch") => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.startsWith("api/launch/defaults")) {
        const mode = new URL(url, "http://x/").searchParams.get("mode");
        return json({
          robots: ["hamilton", "gauss"],
          session: mode === "mapping" ? "spot-default" : "spot-reloc-default",
          base_station: "sequoia",
          bs_session: "base_station_default",
          sessions: ["spot-default", "spot-reloc", "spot-reloc-default", "base_station_default", "base_station_fleetlink"],
          link_networks: ["mit_wifi", "silvus"],
          link_modes: ["router", "fleetlink"],
          link_mode: "router",
          last,
        });
      }
      if (url === "api/maps") return json(MAPS);
      if (url === "api/config") return json({local_machine: "sequoia"});
      if (init?.method === "POST") return json({job_id: "j1"});
      return json({});
    });
    vi.stubGlobal("fetch", fetchMock);
    useFleet.setState({machines: MACHINES as never, loaded: true});
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    act(() =>
      root!.render(
        <MemoryRouter initialEntries={[path]}>
          <LaunchPage />
        </MemoryRouter>,
      ),
    );
    return fetchMock;
  };
  const sel = (label: string) => document.querySelector<HTMLSelectElement>(`select[aria-label="${label}"]`)!;
  const input = (label: string) => document.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!;

  it("prefills prior, sessions, robots, mode and a bumped experiment name", async () => {
    const fetchMock = mount(LAST);
    for (let i = 0; i < 4; i++) await flush();
    // the last launch was relocalize: the page switched to it
    expect(fetchMock.mock.calls.some(c => String(c[0]).includes("mode=relocalize"))).toBe(true);
    expect(sel("Prior map").value).toBe("sequoia\u0000/home/swarm/adt4_output/courtyard_fused");
    expect(input("Experiment name").value).toBe("courtyard_reloc_v2");
    expect(input("Robot session").placeholder).toBe("spot-reloc");
    expect(input("Base station session").placeholder).toBe("base_station_fleetlink");
    expect(sel("Robot link mode").value).toBe("fleetlink");
    const checked = [...document.querySelectorAll<HTMLInputElement>(".robot-pick input")].filter(i => i.checked);
    expect(checked).toHaveLength(1);
    expect(document.querySelector('[data-testid="launch-last"]')!.textContent).toContain("courtyard_fused");

    // launching sends the remembered prior
    const launch = [...document.querySelectorAll("button")].find(b => b.textContent?.trim() === "Launch")!;
    await act(async () => launch.click());
    await flush();
    const post = fetchMock.mock.calls.find(c => c[0] === "api/launch")!;
    const body = JSON.parse(String((post[1] as RequestInit).body));
    expect(body).toMatchObject({
      prior_path: "/home/swarm/adt4_output/courtyard_fused",
      prior_source: "sequoia",
      mode: "relocalize",
      experiment: "courtyard_reloc_v2",
      robots: ["gauss"],
      session: "spot-reloc",
    });
  });

  it("Relaunch settings restores the last launch after the form was changed", async () => {
    mount(LAST);
    for (let i = 0; i < 4; i++) await flush();
    act(() => {
      const s = sel("Prior map");
      s.value = "";
      s.dispatchEvent(new Event("change", {bubbles: true}));
    });
    expect(sel("Prior map").value).toBe("");
    const btn = [...document.querySelectorAll("button")].find(b => b.textContent?.includes("Relaunch settings"))!;
    act(() => btn.click());
    expect(sel("Prior map").value).toBe("sequoia\u0000/home/swarm/adt4_output/courtyard_fused");
  });

  it("keeps a remembered prior the maps listing no longer has", async () => {
    mount({...LAST, prior_path: "/home/swarm/adt4_output/gone_map"});
    for (let i = 0; i < 4; i++) await flush();
    expect(sel("Prior map").value).toBe("sequoia\u0000/home/swarm/adt4_output/gone_map");
    expect(sel("Prior map").selectedOptions[0].textContent).toContain("not listed");
  });

  it("without a last launch uses the topology defaults", async () => {
    mount(null);
    for (let i = 0; i < 3; i++) await flush();
    expect(sel("Prior map").value).toBe("");
    expect(input("Robot session").placeholder).toBe("spot-default");
    expect(input("Experiment name").value).toMatch(/^map_\d{8}_\d{4}$/);
    expect(document.querySelector('[data-testid="launch-last"]')).toBeNull();
  });

  it("a Launch-with-map link keeps its own prior", async () => {
    mount(LAST, "/launch?prior=/home/swarm/adt4_output/other_map&source=sequoia&mode=relocalize");
    for (let i = 0; i < 4; i++) await flush();
    expect(sel("Prior map").value).toBe("sequoia\u0000/home/swarm/adt4_output/other_map");
    expect(input("Robot session").placeholder).toBe("spot-reloc");
  });
});
