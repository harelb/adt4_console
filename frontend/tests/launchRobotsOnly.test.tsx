import {afterEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {MemoryRouter} from "react-router";
import {LaunchPage} from "../src/pages/Launch";
import {useFleet} from "../src/stores/fleet";
import {platformEquivalent, platformSession, picksForPlatforms, sessionFitsPlatform, sessionPlatform} from "../src/lib/launchSessions";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const json = (o: unknown, status = 200) => new Response(JSON.stringify(o), {status, headers: {"Content-Type": "application/json"}});
const flush = () => act(async () => void (await new Promise(r => setTimeout(r, 0))));
const settle = async () => {
  for (let i = 0; i < 5; i++) await flush();
};

const SESSIONS = [
  "spot-default",
  "spot_relocalize-relocalize",
  "spot_relocalize-relocalize_awcd",
  "phoenix-default",
  "phoenix_relocalize-relocalize",
  "phoenix_relocalize-relocalize_awcd",
  "spot_grstaps-grstaps",
  "base_station_web_grstaps_repair-grstaps_repair",
];
const MODES = {
  "spot-default": "mapping",
  "phoenix-default": "mapping",
  "spot_relocalize-relocalize": "relocalize",
  "spot_relocalize-relocalize_awcd": "relocalize",
  "phoenix_relocalize-relocalize": "relocalize",
  "phoenix_relocalize-relocalize_awcd": "relocalize",
};
// launch_memory.json when gauss was relaunched alone on 2026-11-11
const NOV11_MEMORY = {
  robots: ["gauss"],
  session: "spot_relocalize-relocalize_awcd",
  sessions: {},
  bs_session: "base_station_web_grstaps_repair-grstaps_repair",
  base_only: false,
  experiment: "nov11_reloc_awcd",
  mode: "relocalize",
  prior_path: "/home/rrg/adt4_output/nov11_fused_awcd",
  prior_source: "willow",
  record: true,
  base_station: "willow",
};

describe("session platform helpers", () => {
  it("knows a session's platform and maps it to the other one", () => {
    expect(sessionPlatform("spot_relocalize-relocalize_awcd")).toBe("spot");
    expect(sessionPlatform("phoenix-default")).toBe("phoenix");
    expect(sessionPlatform("base_station_web-default")).toBeUndefined();
    expect(sessionFitsPlatform("spot-default", "phoenix")).toBe(false);
    expect(sessionFitsPlatform("base_station_web-default", "phoenix")).toBe(true);
    expect(platformEquivalent("spot_relocalize-relocalize_awcd", "phoenix", SESSIONS)).toBe("phoenix_relocalize-relocalize_awcd");
    expect(platformEquivalent("spot_grstaps-grstaps", "phoenix", SESSIONS)).toBeUndefined();
    expect(picksForPlatforms({phoenix: "spot-default", spot: "spot-default"})).toEqual({spot: "spot-default"});
  });
  it("replays tonight's launch memory: gauss gets the phoenix session", () => {
    const presets = {spot: "spot_relocalize-relocalize", phoenix: "phoenix_relocalize-relocalize"};
    expect(platformSession("phoenix", NOV11_MEMORY.sessions, NOV11_MEMORY.session, presets, SESSIONS)).toBe("phoenix_relocalize-relocalize_awcd");
    // no phoenix twin: the platform's preset, never the Spot session
    expect(platformSession("phoenix", {}, "spot_grstaps-grstaps", presets, SESSIONS)).toBe("phoenix_relocalize-relocalize");
    // a remembered pick of the wrong platform is ignored
    expect(platformSession("phoenix", {phoenix: "spot-default"}, "", presets, SESSIONS)).toBe("phoenix_relocalize-relocalize");
  });
});

const MACHINES = {
  network: "silvus",
  machines: [
    {name: "euclid", role: "robot", ip: "192.0.2.11", user: "swarm", online: true, platform_type: "spot"},
    {name: "hamilton", role: "robot", ip: "192.0.2.12", user: "swarm", online: true, platform_type: "spot"},
    {name: "gauss", role: "robot", ip: "192.0.2.13", user: "swarm", online: true, platform_type: "phoenix"},
    {name: "willow", role: "base_station", ip: "192.0.2.4", user: "rrg", online: true, local: true},
  ],
};

describe("Launch form: robots only and per-robot restart", () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    vi.unstubAllGlobals();
  });

  const mount = (last: unknown = NOV11_MEMORY) => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.startsWith("api/launch/defaults")) {
        const reloc = new URL(url, "http://x/").searchParams.get("mode") === "relocalize";
        return json({
          mode: reloc ? "relocalize" : "mapping",
          robots: ["euclid", "hamilton", "gauss"],
          session: reloc ? "spot_relocalize-relocalize" : "spot-default",
          base_station: "willow",
          bs_session: "base_station_web_grstaps_repair-grstaps_repair",
          sessions: SESSIONS,
          session_modes: MODES,
          robot_platforms: {euclid: "spot", hamilton: "spot", gauss: "phoenix"},
          platform_sessions: reloc ? {spot: "spot_relocalize-relocalize", phoenix: "phoenix_relocalize-relocalize"} : {spot: "spot-default", phoenix: "phoenix-default"},
          last,
        });
      }
      if (url === "api/launch/active") return json({active: {experiment: "nov11_reloc_awcd", robots: ["euclid", "hamilton", "gauss"], prior: null}, default_sources: []});
      if (url === "api/maps") return json({base: "willow", machines: {}});
      if (url === "api/config") return json({local_machine: "willow"});
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
        <MemoryRouter initialEntries={["/launch"]}>
          <LaunchPage />
        </MemoryRouter>,
      ),
    );
    return fetchMock;
  };
  const input = (label: string) => document.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`);
  const buttons = (text: string) => [...document.querySelectorAll("button")].filter(b => b.textContent?.trim() === text);
  const lastPost = (fetchMock: ReturnType<typeof mount>) => {
    const post = fetchMock.mock.calls.filter(c => c[0] === "api/launch").pop()!;
    return JSON.parse(String((post[1] as RequestInit).body));
  };

  it("tonight's memory launches gauss with the phoenix session", async () => {
    const fetchMock = mount();
    await settle();
    expect(input("Robot session")!.placeholder).toBe("phoenix_relocalize-relocalize_awcd");
    await act(async () => buttons("Launch")[0].click());
    await flush();
    expect(lastPost(fetchMock)).toMatchObject({
      robots: ["gauss"],
      session: "phoenix_relocalize-relocalize_awcd",
      sessions: {phoenix: "phoenix_relocalize-relocalize_awcd"},
      robots_only: false,
    });
  });

  it("the gauss picker lists only Husky sessions", async () => {
    mount();
    await settle();
    await act(async () => input("Robot session")!.focus());
    const items = [...(input("Robot session")!.closest(".fuzzy")?.querySelectorAll(".fuzzy-list li") ?? [])].map(li => li.textContent ?? "");
    expect(items.sort()).toEqual(["phoenix_relocalize-relocalize", "phoenix_relocalize-relocalize_awcd"]);
  });

  it("Robots only relaunches the selected robots and keeps the base", async () => {
    const fetchMock = mount();
    await settle();
    const ro = [...document.querySelectorAll("button")].find(b => b.textContent?.includes("Robots only (base keeps running)"))!;
    await act(async () => ro.click());
    expect(document.body.textContent).toContain("base keeps running");
    await act(async () => buttons("Relaunch robots")[0].click());
    await flush();
    expect(lastPost(fetchMock)).toMatchObject({robots: ["gauss"], robots_only: true, save_first: false, base_only: false, base_station: "willow"});
  });

  it("Restart on a robot relaunches it alone with its platform's session", async () => {
    const fetchMock = mount();
    await settle();
    const card = [...document.querySelectorAll(".robot-pick label")].find(l => l.textContent?.includes("gauss"))!;
    const restart = [...card.querySelectorAll("button")].find(b => b.textContent === "Restart")!;
    await act(async () => restart.click());
    const panel = document.querySelector('[data-testid="restart-panel"]')!;
    expect(panel.textContent).toContain("phoenix_relocalize-relocalize_awcd");
    expect(input("Restart experiment")!.value).toBe("nov11_reloc_awcd");
    // the click did not toggle the robot's checkbox
    expect(card.querySelector<HTMLInputElement>("input[type=checkbox]")!.checked).toBe(true);
    const save = [...panel.querySelectorAll("label.toggle")].find(l => l.textContent?.includes("Stop & save first"))!;
    await act(async () => save.querySelector("input")!.click());
    await act(async () => buttons("Restart gauss")[0].click());
    await flush();
    expect(lastPost(fetchMock)).toMatchObject({
      robots: ["gauss"],
      session: "phoenix_relocalize-relocalize_awcd",
      sessions: {phoenix: "phoenix_relocalize-relocalize_awcd"},
      robots_only: true,
      save_first: true,
      experiment: "nov11_reloc_awcd",
      mode: "relocalize",
      prior_path: "/home/rrg/adt4_output/nov11_fused_awcd",
      record: true,
    });
  });

  it("Restart on a Spot uses the Spot session", async () => {
    const fetchMock = mount({...NOV11_MEMORY, robots: ["euclid", "gauss"]});
    await settle();
    const card = [...document.querySelectorAll(".robot-pick label")].find(l => l.textContent?.includes("euclid"))!;
    await act(async () => [...card.querySelectorAll("button")].find(b => b.textContent === "Restart")!.click());
    await act(async () => buttons("Restart euclid")[0].click());
    await flush();
    expect(lastPost(fetchMock)).toMatchObject({robots: ["euclid"], sessions: {spot: "spot_relocalize-relocalize_awcd"}, robots_only: true});
  });
});
