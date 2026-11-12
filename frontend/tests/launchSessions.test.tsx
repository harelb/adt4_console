import {afterEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {MemoryRouter} from "react-router";
import {LaunchPage} from "../src/pages/Launch";
import {useFleet} from "../src/stores/fleet";
import {platformOf, platformSession, platformsOf, sessionsFor} from "../src/lib/launchSessions";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const json = (o: unknown, status = 200) => new Response(JSON.stringify(o), {status, headers: {"Content-Type": "application/json"}});
const flush = () => act(async () => void (await new Promise(r => setTimeout(r, 0))));

describe("launchSessions helpers", () => {
  it("maps robots to platforms (backend first, else platform_type)", () => {
    expect(platformOf("gauss", {gauss: "phoenix"})).toBe("phoenix");
    expect(platformOf("x", undefined, "husky")).toBe("phoenix");
    expect(platformOf("x", undefined, undefined)).toBe("spot");
    expect(platformsOf(["a", "b", "c"], r => (r === "b" ? "phoenix" : "spot"))).toEqual(["spot", "phoenix"]);
  });
  it("filters sessions per platform and picks a default", () => {
    const all = ["spot-default", "spot_relocalize-relocalize", "phoenix-default", "phoenix_relocalize-relocalize", "base_station-default"];
    expect(sessionsFor(all, "phoenix")).toEqual(["phoenix-default", "phoenix_relocalize-relocalize"]);
    expect(platformSession("spot", {}, "spot_relocalize-relocalize", {spot: "spot-default"})).toBe("spot_relocalize-relocalize");
    expect(platformSession("phoenix", {}, "spot-default", {phoenix: "phoenix-default"})).toBe("phoenix-default");
    expect(platformSession("phoenix", {phoenix: "phoenix-x"}, "", {phoenix: "phoenix-default"})).toBe("phoenix-x");
  });
});

const MACHINES = {
  network: "silvus",
  machines: [
    {name: "hamilton", role: "robot", ip: "198.51.100.1", user: "swarm", online: true, platform_type: "spot"},
    {name: "gauss", role: "robot", ip: "203.0.113.179", user: "swarm", online: true, platform_type: "husky"},
    {name: "sequoia", role: "base_station", ip: "198.51.100.9", user: "swarm", online: true, local: true},
  ],
};

describe("Launch form: per-platform sessions and base station only", () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    vi.unstubAllGlobals();
  });

  const mount = (last: unknown = null) => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.startsWith("api/launch/defaults")) {
        const mode = new URL(url, "http://x/").searchParams.get("mode");
        const reloc = mode === "relocalize";
        return json({
          robots: ["hamilton", "gauss"],
          session: reloc ? "spot_relocalize-relocalize" : "spot-default",
          base_station: "sequoia",
          bs_session: "base_station_web-default",
          sessions: ["spot-default", "spot_relocalize-relocalize", "phoenix-default", "phoenix_relocalize-relocalize", "base_station_web-default"],
          robot_platforms: {hamilton: "spot", gauss: "phoenix"},
          platform_sessions: reloc
            ? {spot: "spot_relocalize-relocalize", phoenix: "phoenix_relocalize-relocalize"}
            : {spot: "spot-default", phoenix: "phoenix-default"},
          last,
        });
      }
      if (url === "api/maps") return json({base: "sequoia", machines: {}});
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
        <MemoryRouter initialEntries={["/launch"]}>
          <LaunchPage />
        </MemoryRouter>,
      ),
    );
    return fetchMock;
  };
  const input = (label: string) => document.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`);
  const button = (text: string) => [...document.querySelectorAll("button")].find(b => b.textContent?.trim() === text)!;
  const posted = async (fetchMock: ReturnType<typeof mount>, label: string) => {
    await act(async () => button(label).click());
    await flush();
    const post = fetchMock.mock.calls.find(c => c[0] === "api/launch")!;
    return JSON.parse(String((post[1] as RequestInit).body));
  };

  it("shows one session picker per platform and sends sessions per platform", async () => {
    const fetchMock = mount();
    for (let i = 0; i < 3; i++) await flush();
    expect(input("Robot session")).toBeNull();
    expect(input("Spot session")!.placeholder).toBe("spot-default");
    expect(input("Husky session")!.placeholder).toBe("phoenix-default");
    const body = await posted(fetchMock, "Launch");
    expect(body).toMatchObject({
      robots: ["hamilton", "gauss"],
      sessions: {spot: "spot-default", phoenix: "phoenix-default"},
      session: "spot-default",
      base_only: false,
    });
  });

  it("a remembered launch restores per-platform sessions", async () => {
    mount({robots: ["hamilton", "gauss"], mode: "mapping", experiment: "e1", base_station: "sequoia", bs_session: "base_station_web-default",
      sessions: {spot: "spot_relocalize-relocalize", phoenix: "phoenix_relocalize-relocalize"}});
    for (let i = 0; i < 3; i++) await flush();
    expect(input("Spot session")!.placeholder).toBe("spot_relocalize-relocalize");
    expect(input("Husky session")!.placeholder).toBe("phoenix_relocalize-relocalize");
  });

  it("Robots: none launches only the base station", async () => {
    const fetchMock = mount();
    for (let i = 0; i < 3; i++) await flush();
    const none = [...document.querySelectorAll("button")].find(b => b.textContent?.includes("None (base station only)"))!;
    act(() => none.click());
    expect(document.querySelector(".robot-pick")).toBeNull();
    expect(input("Spot session")).toBeNull();
    const body = await posted(fetchMock, "Launch base station");
    expect(body).toMatchObject({robots: [], base_only: true, base_station: "sequoia", bs_session: "base_station_web-default", sessions: {}});
  });

  it("a remembered base-only launch reopens in base-only mode", async () => {
    mount({robots: ["hamilton"], mode: "mapping", experiment: "e1", base_station: "sequoia", bs_session: "base_station_web-default", base_only: true});
    for (let i = 0; i < 3; i++) await flush();
    expect(document.querySelector(".robot-pick")).toBeNull();
    expect(button("Launch base station")).toBeTruthy();
  });
});
