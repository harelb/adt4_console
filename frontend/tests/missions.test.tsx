// Missions page (mission logging C1/C2): helpers, list, tabs and the replay scrubber.
import {afterEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {createMemoryRouter, RouterProvider} from "react-router";
import {MissionsPage} from "../src/pages/Missions";
import {MissionReplay} from "../src/pages/MissionReplay";
import {
  activePlans, changesUntil, fmtClock, fmtDuration, laneOf, lanes, missionRel, poseAt, relocAt, replayBounds,
  type MissionEvent, type Replay,
} from "../src/lib/missionLog"; // prettier-ignore
import {useLogbook} from "../src/stores/logbook";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const T = 1794450000;
const MID = "m20261111-200500_bbbb";
const ev = (over: Partial<MissionEvent>): MissionEvent => ({t_base: T, t_wall: T, _machine: "base", source: "console", type: "x", payload: {}, ...over});

const REPLAY: Replay = {
  mission_id: MID,
  t_start: T,
  t_end: T + 120,
  t_range: [T - 5, T + 125],
  poses: {euclid: [[T, 0, 0, 0], [T + 10, 0, 0, 0], [T + 50, 10, 0, 0]]},
  plans: [{t: T + 8, robot: "euclid", plan_id: "plan-0001", path: [[0, 0], [5, 0], [10, 0]], len_m: 10}],
  changes: [{t: T + 40, op: "add_object", symbol: "O(99)", label: "mug", xy: [5, 2]}],
  reloc: [{t: T - 100, type: "reloc.relocalized", robot: "euclid", state: "relocalized"}],
  stages: [{t: T + 10, robot: "euclid", to: "executing"}, {t: T + 60, robot: "euclid", to: "done"}],
  map: {objects: [[3, 3, "chair"]], places: [[1, 1]]},
};

describe("mission log helpers", () => {
  it("formats and lanes", () => {
    expect(fmtDuration(125)).toBe("2 min 5 s");
    expect(fmtClock(T + 65, T)).toBe("1:05");
    expect(laneOf(ev({type: "chat.user"}))).toBe("chat");
    expect(laneOf(ev({source: "omniplanner", type: "plan.solved"}))).toBe("planner");
    expect(laneOf(ev({_machine: "euclid", type: "action.start"}))).toBe("euclid");
    expect(lanes([ev({_machine: "euclid"}), ev({type: "goal.sent"})])).toEqual(["chat", "planner", "base", "euclid"]);
    expect(missionRel(MID, `missions/${MID}/images/goals/g/x.jpg`)).toBe("images/goals/g/x.jpg");
  });
  it("replay state at a time", () => {
    expect(poseAt(REPLAY.poses.euclid, T - 1)).toBeNull();
    expect(poseAt(REPLAY.poses.euclid, T + 30)!.x).toBeCloseTo(5);
    expect(poseAt(REPLAY.poses.euclid, T + 999)!.x).toBe(10);
    expect(activePlans(REPLAY.plans, T + 5)).toEqual({});
    expect(activePlans(REPLAY.plans, T + 9).euclid.plan_id).toBe("plan-0001");
    expect(changesUntil(REPLAY.changes, T + 39)).toHaveLength(0);
    expect(changesUntil(REPLAY.changes, T + 41)).toHaveLength(1);
    expect(relocAt(REPLAY.reloc, T).euclid).toBe("relocalized");
    const [x0, , x1] = replayBounds(REPLAY);
    expect(x0).toBeLessThan(0);
    expect(x1).toBeGreaterThan(10);
  });
});

describe("Missions page", () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  const posts: string[] = [];
  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    vi.unstubAllGlobals();
    posts.length = 0;
    useLogbook.setState({state: null, error: null});
  });
  const LIST = {
    sessions: [{
      session_id: "s20261111-200000_aaaa", experiment: "fx_exp", state: "stopped", t_start: T - 400, robots: ["euclid"], mode: "relocalize",
      reloc: {euclid: {state: "relocalized", operator: "harel"}},
      missions: [{mission_id: MID, name: "fixture run", t_start: T, t_end: T + 120, duration_s: 120, robots: ["euclid"], n_goals: 1, goals_done: 1, goals_failed: 0, check: "pass", collect: "done", size: 12345}],
    }],
    current: null,
  }; // prettier-ignore
  const DETAIL = {
    summary: LIST.sessions[0].missions[0],
    mission: {mission_id: MID, t_start: T, t_end: T + 120, goals: ["g1"], robots: ["euclid"]},
    manifest: {clock_offsets: {euclid: {max_abs_s: 0.004}}},
    report: {mission_id: MID, status: "pass", t_checked: T + 200, export_blocked: false, counts: {pass: 13},
             checks: [{id: "clock", title: "Clock offsets sampled and small", status: "pass", detail: "euclid: 0.004 s max over 6"}]},
    collect: {state: "done", machines: {base: {state: "collected"}, euclid: {state: "collected"}}},
  }; // prettier-ignore
  const EVENTS = {
    events: [ev({type: "mission.start"}), ev({type: "goal.sent", t_base: T + 5, goal_id: "g1"}), ev({_machine: "euclid", source: "spot_executor", type: "lease.intervention", t_base: T + 30, payload: {kind: "manual"}})],
    total: 3, t_range: [T, T + 120], facets: {machine: {base: 2, euclid: 1}, source: {}, type: {}},
  }; // prettier-ignore
  const stub = () =>
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const u = String(url);
        if (init?.method === "POST") {
          posts.push(u);
          return new Response(JSON.stringify(DETAIL.report));
        }
        if (/api\/logbook\/missions(\?.*)?$/.test(u)) return new Response(JSON.stringify(LIST));
        if (u.includes("/events")) return new Response(JSON.stringify(EVENTS));
        if (u.includes("/goals")) return new Response(JSON.stringify({goals: [{goal_id: "g1", kind: "goto", robot: "euclid", t_sent: T + 5, stages: [{t: T + 10, robot: "euclid", to: "executing"}], robots: {euclid: {plan_id: "plan-0001", planned_m: 10, driven_m: 9.8, result: "done", time_to_reach_s: 55, leases: 1, stalls: 0, repairs: 0}}}]}));
        if (u.includes("/replay")) return new Response(JSON.stringify(REPLAY));
        if (u.endsWith(`/missions/${MID}`)) return new Response(JSON.stringify(DETAIL));
        if (u.includes("api/logbook")) return new Response(JSON.stringify({session: null, reloc: {}, gate: {ok: true, reasons: [], blocking: []}, mission: null}));
        return new Response("{}", {status: 404});
      }),
    );
  const mount = async (path: string) => {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    const router = createMemoryRouter([{path: "/missions/:id?", element: <MissionsPage />}], {initialEntries: [path]});
    await act(async () => root!.render(<RouterProvider router={router} />));
    await act(async () => new Promise(r => setTimeout(r, 30)));
    return host;
  };
  const click = async (el: Element) => {
    await act(async () => void (el as HTMLElement).click());
    await act(async () => new Promise(r => setTimeout(r, 40)));
  };

  it("lists missions by session with check and collect status", async () => {
    stub();
    const el = await mount("/missions");
    expect(el.textContent).toContain("s20261111-200000_aaaa");
    expect(el.textContent).toContain("fixture run");
    expect(el.querySelector(`[data-testid="mission-row-${MID}"]`)!.textContent).toContain("pass");
    expect(el.textContent).toContain("Start mission");
  });

  it("detail: timeline, goals, check and replay tabs", async () => {
    stub();
    const el = await mount(`/missions/${MID}`);
    expect(el.textContent).toContain("fixture run");
    expect(el.querySelectorAll(".mlane").length).toBeGreaterThanOrEqual(4);
    expect(el.querySelectorAll(".mdot").length).toBe(3);
    await click(el.querySelector('.mdot[aria-label="lease.intervention"]')!);
    expect(el.querySelector(".mevent-detail")!.textContent).toContain("manual");
    expect(el.textContent).toContain("euclid clock ±4 ms");
    const tab = (name: string) => [...el.querySelectorAll('[role="tab"]')].find(b => b.textContent === name)!;
    await click(tab("Goals"));
    expect(el.textContent).toContain("plan-0001");
    expect(el.textContent).toContain("10 m / 9.8 m");
    await click(tab("Check"));
    expect(el.querySelector('[data-check="clock"]')!.textContent).toContain("pass");
    await click(el.querySelector(".mtab button.btn-primary")!);
    expect(posts.some(p => p.endsWith(`/missions/${MID}/check`))).toBe(true);
    await click(tab("Replay"));
    expect(el.querySelector('svg[aria-label="Mission replay map"]')).not.toBeNull();
  });
});

describe("replay scrubber", () => {
  it("draws the robot, its plan and the changes applied by the scrubbed time", async () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    await act(async () => root.render(<MissionReplay replay={REPLAY} />));
    const scrub = host.querySelector('input[aria-label="Replay time"]') as HTMLInputElement;
    expect(host.querySelectorAll('g[data-robot="euclid"] polyline').length).toBe(0); // t0: no plan yet
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    await act(async () => {
      setter.call(scrub, String(T + 45));
      scrub.dispatchEvent(new Event("input", {bubbles: true}));
    });
    expect(host.querySelector('[data-testid="replay-clock"]')!.textContent).toContain("0:45");
    expect(host.querySelectorAll('g[data-robot="euclid"] polyline').length).toBe(2); // plan + trail
    expect(host.textContent).toContain("1 scene changes");
    expect(host.textContent).toContain("executing");
    act(() => root.unmount());
    host.remove();
  });
});
