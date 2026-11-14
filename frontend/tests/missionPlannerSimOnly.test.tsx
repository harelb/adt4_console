// The mission planner (/mission) only simulates: its chat has no Send or
// auto-send, its Mission tab no Execute or robot binding; "Deploy in Robot
// view" hands the mission to /viewer, which keeps Execute.
import {afterEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {MemoryRouter, Route, Routes, useLocation} from "react-router";
import {GoalCard, toChatItem, type ChatItem} from "../src/viewer/ChatPanel";
import {disabledWhy, MissionPanel} from "../src/viewer/MissionPanel";
import {useMission, type Mission} from "../src/stores/mission";
import {useRobots} from "../src/stores/robots";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLDivElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = host = null;
  vi.unstubAllGlobals();
});
function render(el: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(el));
  return host;
}
const buttons = (h: HTMLElement) => [...h.querySelectorAll("button")].map(b => b.textContent ?? "");

const mission: Mission = {
  id: "m1", name: "patrol", text: "patrol", src: null, planner: "llm", goal: null,
  agents: [{id: "spot1", kind: "spot", start: [0, 0], binding: null}],
  tasks: [{id: "t1", agent: "spot1", type: "goto", target: "P(1)"}],
  geofences: [], planted: [], created_at: 0, revision: 1, parent: null, saved: true,
} as unknown as Mission;

function Where() {
  const loc = useLocation();
  return <div data-testid="where">{loc.pathname + loc.search}</div>;
}

describe("mission planner is simulation only", () => {
  it("goal card without onSend has no Send button", () => {
    const item = toChatItem({type: "goal", kind: "pddl", body: {goal: "(visited-object o2)"}, robot: "hamilton"}) as Extract<ChatItem, {type: "goal"}>;
    const h = render(<GoalCard it={item} robot="hamilton" src={null} />);
    expect(h.querySelector('[data-testid="goal-send"]')).toBeNull();
    expect(h.querySelector('[data-testid="goal-send-viewer-only"]')).not.toBeNull();
  });

  it("disabledWhy in the planner never asks to bind robots", () => {
    expect(disabledWhy(0, 0, false, true)).toBeNull();
    expect(disabledWhy(1, 0, false, true)).toMatch(/Simulate and Deploy are off/);
    expect(disabledWhy(0, 0, false)).toMatch(/bind agents/);
  });

  it("planner Mission tab: no Execute or binding; Deploy opens the mission in /viewer", () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({missions: []}), {status: 200})));
    useRobots.setState({state: {hamilton: {}}} as never);
    useMission.setState({current: mission, preview: {issues: [], makespan: 10, tasks: {}, agents: {spot1: {start: [0, 0], end_s: 10, distance_m: 1}}, geo: null} as never, run: null, list: [], roster: mission.agents});
    const h = render(
      <MemoryRouter initialEntries={["/mission"]}>
        <Routes>
          <Route path="/mission" element={<MissionPanel plannerOnly />} />
          <Route path="/viewer" element={<Where />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(buttons(h).some(t => t.includes("Execute"))).toBe(false);
    expect(h.querySelector('select[aria-label="Real robot"]')).toBeNull();
    expect(buttons(h).some(t => t.includes("+ hamilton"))).toBe(false);
    expect(h.querySelector('[data-testid="planner-sim-only"]')).not.toBeNull();
    expect(h.querySelector('[data-testid="mission-export"]')?.getAttribute("href")).toContain("api/missions/m1/export");
    act(() => (h.querySelector('[data-testid="deploy-in-viewer"]') as HTMLButtonElement).click());
    expect(h.querySelector('[data-testid="where"]')?.textContent).toBe("/viewer?mission=m1");
  });

  it("Robot view Mission tab keeps Execute and robot binding", () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({missions: []}), {status: 200})));
    useRobots.setState({state: {hamilton: {}}} as never);
    useMission.setState({current: mission, preview: {issues: [], makespan: 10, tasks: {}, agents: {spot1: {start: [0, 0], end_s: 10, distance_m: 1}}, geo: null} as never, run: null, list: [], roster: mission.agents});
    const h = render(
      <MemoryRouter initialEntries={["/viewer"]}>
        <MissionPanel />
      </MemoryRouter>,
    );
    expect(buttons(h).some(t => t.includes("Execute"))).toBe(true);
    expect(h.querySelector('select[aria-label="Real robot"]')).not.toBeNull();
    expect(h.querySelector('[data-testid="deploy-in-viewer"]')).toBeNull();
  });
});
