import {afterEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {MemoryRouter} from "react-router";
import {LaunchPage} from "../src/pages/Launch";
import {useFleet} from "../src/stores/fleet";
import {picksForMode, sessionForMode, sessionMismatch, sessionsForMode} from "../src/lib/launchSessions";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const json = (o: unknown, status = 200) => new Response(JSON.stringify(o), {status, headers: {"Content-Type": "application/json"}});
const flush = () => act(async () => void (await new Promise(r => setTimeout(r, 0))));

const SESSIONS = [
  "base_station_web_repair-default",
  "phoenix-default",
  "phoenix_relocalize-relocalize",
  "spot-default",
  "spot-indoor_image_logging",
  "spot_relocalize-relocalize",
  "spot_relocalize-relocalize_awcd",
  "spot_sim_custom-default",
];
const MODES: Record<string, string> = {
  "phoenix-default": "mapping",
  "phoenix_relocalize-relocalize": "relocalize",
  "spot-default": "mapping",
  "spot-indoor_image_logging": "mapping",
  "spot_relocalize-relocalize": "relocalize",
  "spot_relocalize-relocalize_awcd": "relocalize",
};

describe("session vs launch mode helpers", () => {
  it("flags only a session of the other mode", () => {
    expect(sessionMismatch("spot-indoor_image_logging", "relocalize", MODES)).toBe(true);
    expect(sessionMismatch("spot_relocalize-relocalize", "mapping", MODES)).toBe(true);
    expect(sessionMismatch("spot_relocalize-relocalize", "relocalize", MODES)).toBe(false);
    expect(sessionMismatch("spot_sim_custom-default", "relocalize", MODES)).toBe(false);
    expect(sessionMismatch("spot-default", "relocalize", undefined)).toBe(false);
  });
  it("lists the mode's sessions unless showing all", () => {
    expect(sessionsForMode(SESSIONS, "relocalize", MODES, false)).toEqual([
      "phoenix_relocalize-relocalize",
      "spot_relocalize-relocalize",
      "spot_relocalize-relocalize_awcd",
    ]);
    expect(sessionsForMode(SESSIONS, "mapping", MODES, true)).toEqual(SESSIONS);
    // a hand-picked session (show all) stays listed; old backends: everything
    expect(sessionsForMode(SESSIONS, "mapping", MODES, false, "spot_sim_custom-default")).toContain("spot_sim_custom-default");
    expect(sessionsForMode(SESSIONS, "mapping", undefined, false)).toEqual(SESSIONS);
  });
  it("replaces a remembered session of the other mode with the preset", () => {
    expect(sessionForMode("spot-indoor_image_logging", "relocalize", MODES, "spot_relocalize-relocalize")).toBe("spot_relocalize-relocalize");
    expect(sessionForMode("spot_relocalize-relocalize_awcd", "relocalize", MODES, "spot_relocalize-relocalize")).toBe("spot_relocalize-relocalize_awcd");
    expect(picksForMode({spot: "spot-default", phoenix: "phoenix_relocalize-relocalize"}, "relocalize", MODES)).toEqual({phoenix: "phoenix_relocalize-relocalize"});
  });
});

// willow's ~/.adt4_console/launch_memory.json after the 2026-11-11 field run
const FIELD_RUN_MEMORY = {
  robots: ["euclid", "hamilton"],
  session: "spot-indoor_image_logging",
  experiment: "nov11_b45_v3_map_images_v2",
  mode: "relocalize",
  prior_path: "/home/rrg/adt4_output/nov11_b45_v3_map_images__euclid",
  prior_source: "willow",
  record: true,
  base_station: "willow",
  bs_session: "base_station_web_repair-default",
  sessions: {},
  base_only: false,
};

const MACHINES = {
  network: "silvus",
  machines: [
    {name: "euclid", role: "robot", ip: "198.51.100.2", user: "swarm", online: true, platform_type: "spot"},
    {name: "hamilton", role: "robot", ip: "198.51.100.1", user: "swarm", online: true, platform_type: "spot"},
    {name: "gauss", role: "robot", ip: "203.0.113.179", user: "swarm", online: true, platform_type: "husky"},
    {name: "willow", role: "base_station", ip: "198.51.100.9", user: "rrg", online: true, local: true},
  ],
};

describe("Launch form: robot session follows the launch mode", () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    vi.unstubAllGlobals();
  });

  const mount = (last: unknown = null, robots = ["euclid", "hamilton"]) => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.startsWith("api/launch/defaults")) {
        const mode = new URL(url, "http://x/").searchParams.get("mode");
        const reloc = mode === "relocalize";
        return json({
          mode,
          robots,
          session: reloc ? "spot_relocalize-relocalize" : "spot-default",
          base_station: "willow",
          bs_session: "base_station_web_repair-default",
          sessions: SESSIONS,
          session_modes: MODES,
          robot_platforms: {euclid: "spot", hamilton: "spot", gauss: "phoenix"},
          platform_sessions: reloc
            ? {spot: "spot_relocalize-relocalize", phoenix: "phoenix_relocalize-relocalize"}
            : {spot: "spot-default", phoenix: "phoenix-default"},
          last,
        });
      }
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
  const settle = async () => {
    for (let i = 0; i < 4; i++) await flush();
  };
  const input = (label: string) => document.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`);
  const button = (text: string) => [...document.querySelectorAll("button")].find(b => b.textContent?.trim() === text)!;
  const posted = async (fetchMock: ReturnType<typeof mount>) => {
    await act(async () => button("Launch").click());
    await flush();
    const post = fetchMock.mock.calls.find(c => c[0] === "api/launch")!;
    return JSON.parse(String((post[1] as RequestInit).body));
  };
  const pickerOptions = async (label: string) => {
    await act(async () => input(label)!.focus());
    const items = [...(input(label)!.closest(".fuzzy")?.querySelectorAll(".fuzzy-list li") ?? [])].map(li => li.textContent ?? "");
    await act(async () => input(label)!.blur());
    await act(async () => void document.dispatchEvent(new MouseEvent("mousedown")));
    return items;
  };

  it("the field run's launch memory prefills a relocalize session, not the remembered mapping one", async () => {
    const fetchMock = mount(FIELD_RUN_MEMORY);
    await settle();
    expect(document.querySelector('[aria-label="Launch preset"] .on, [aria-label="Launch preset"] [aria-selected="true"]')?.textContent).toBe("Relocalize");
    expect(input("Robot session")!.placeholder).toBe("spot_relocalize-relocalize");
    const body = await posted(fetchMock);
    expect(body).toMatchObject({
      mode: "relocalize",
      robots: ["euclid", "hamilton"],
      session: "spot_relocalize-relocalize",
      prior_path: "/home/rrg/adt4_output/nov11_b45_v3_map_images__euclid",
      prior_source: "willow",
      bs_session: "base_station_web_repair-default",
    });
  });

  it("keeps a remembered session of the same mode", async () => {
    mount({...FIELD_RUN_MEMORY, session: "spot_relocalize-relocalize_awcd"});
    await settle();
    expect(input("Robot session")!.placeholder).toBe("spot_relocalize-relocalize_awcd");
  });

  it("switching the mode replaces a session of the other mode", async () => {
    mount();
    await settle();
    expect(input("Robot session")!.placeholder).toBe("spot-default");
    await act(async () => button("Relocalize").click());
    await settle();
    expect(input("Robot session")!.placeholder).toBe("spot_relocalize-relocalize");
    await act(async () => button("Mapping").click());
    await settle();
    expect(input("Robot session")!.placeholder).toBe("spot-default");
  });

  it("the picker lists only the mode's sessions, with a show-all escape hatch", async () => {
    mount(FIELD_RUN_MEMORY);
    await settle();
    // Spot robots only: the mode's Spot sessions
    expect((await pickerOptions("Robot session")).sort()).toEqual(["spot_relocalize-relocalize", "spot_relocalize-relocalize_awcd"]);
    const toggle = [...document.querySelectorAll("label.toggle")].find(l => l.textContent?.includes("Show all sessions"))!;
    expect(toggle.textContent).toContain("relocalize sessions only");
    await act(async () => toggle.querySelector("input")!.click());
    expect((await pickerOptions("Robot session")).sort()).toEqual([...SESSIONS].sort());
    expect(document.body.textContent).toContain("Showing all sessions");
    // the session's mode is shown next to it
    expect(document.querySelector(".field .badge-ok")?.textContent).toBe("relocalize");
  });

  it("per-platform: remembered mapping picks are dropped in relocalize mode", async () => {
    const fetchMock = mount(
      {...FIELD_RUN_MEMORY, robots: ["euclid", "gauss"], session: "spot-default", sessions: {spot: "spot-default", phoenix: "phoenix-default"}},
      ["euclid", "gauss"],
    );
    await settle();
    expect(input("Spot session")!.placeholder).toBe("spot_relocalize-relocalize");
    expect(input("Husky session")!.placeholder).toBe("phoenix_relocalize-relocalize");
    expect(await pickerOptions("Husky session")).toEqual(["phoenix_relocalize-relocalize"]);
    const body = await posted(fetchMock);
    expect(body.sessions).toEqual({spot: "spot_relocalize-relocalize", phoenix: "phoenix_relocalize-relocalize"});
  });
});
