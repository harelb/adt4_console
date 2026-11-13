import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";

vi.mock("../src/lib/api", () => ({
  getJSON: vi.fn(async () => {
    throw new Error("404");
  }),
}));

import {GoalStatus, GoalStatusView} from "../src/viewer/GoalStatus";
import {NOTABLE, robotLine, stageLabel, stageTone, trackNote, useGoalTracks, type GoalTrackView, type RobotTrackView} from "../src/lib/goalTrack";
import {ws} from "../src/ws";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

function robot(over: Partial<RobotTrackView>): RobotTrackView {
  return {
    robot: "euclid", named: true, plan_id: "0026942e", actions: ["FOLLOW", "GAZE"], n: 2, done: 0,
    delivered: null, received: 100, received_how: "action result", paused: false, lease: null, lease_owner: "",
    stage: "executing", text: "executing action 1/2 (FOLLOW)", events: [{t: 100, text: "received by euclid (action result)"}],
    ...over,
  };
}

function view(over: Partial<GoalTrackView>): GoalTrackView {
  return {
    id: "g1", t: 90, robot: "euclid", kind: "pddl", via: "goal_manager", summary: "(holding euclid o1)",
    planner: "planned", planner_error: "", stage: "executing", text: "", robots: {euclid: robot({})},
    events: [{t: 90, text: "goal sent to euclid via goal_manager"}], ...over,
  };
}

let root: Root;
let host: HTMLDivElement;
beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
  useGoalTracks.setState({tracks: {}});
});

describe("goal status helpers", () => {
  it("maps stages to tones and labels", () => {
    expect(stageTone("executing")).toBe("ok");
    expect(stageTone("paused")).toBe("warn");
    expect(stageTone("lease_wait")).toBe("warn");
    expect(stageTone("plan_failed")).toBe("err");
    expect(stageLabel("lease_wait")).toBe("waiting: lease");
    expect(NOTABLE.has("paused") && NOTABLE.has("done") && !NOTABLE.has("executing")).toBe(true);
  });

  it("says whether the robot received the plan", () => {
    expect(robotLine(robot({}))).toBe("received · executing action 1/2 (FOLLOW)");
    expect(robotLine(robot({received: null, delivered: null, stage: "planned", text: "planned: 2 actions"}))).toBe("not yet received · planned: 2 actions");
    expect(robotLine(robot({received: null, delivered: 5, stage: "delivered", text: "x"}))).toBe("delivered · x");
  });

  it("writes a transcript note", () => {
    expect(trackNote(view({stage: "paused", text: "euclid: paused at action 1/2"}))).toBe(
      "[operator] status of the goal sent to euclid ((holding euclid o1)): paused — euclid: paused at action 1/2",
    );
  });
});

describe("GoalStatusView", () => {
  it("shows each robot's step and expands to the timeline", () => {
    const v = view({
      stage: "lease_wait",
      robots: {
        euclid: robot({stage: "paused", paused: true, text: "paused at action 1/2"}),
        hamilton: robot({robot: "hamilton", stage: "lease_wait", lease: "wait", text: "waiting — the tablet (bosdyn.android.spotapp x) holds the lease (tablet took over)"}),
      },
    });
    act(() => root.render(<GoalStatusView v={v} />));
    expect(host.querySelector('[data-testid="goal-status-euclid"]')!.textContent).toContain("paused");
    expect(host.querySelector('[data-testid="goal-status-hamilton"]')!.textContent).toContain("tablet took over");
    expect(host.querySelector('[data-testid="goal-status-timeline"]')).toBeNull();
    act(() => (host.querySelector('[data-testid="goal-status-toggle"]') as HTMLButtonElement).click());
    const tl = host.querySelector('[data-testid="goal-status-timeline"]')!;
    expect(tl.textContent).toContain("goal sent to euclid via goal_manager");
    expect(tl.textContent).toContain("plan 0026942e · 2 actions (FOLLOW, GAZE)");
  });

  it("shows a planning failure without robots", () => {
    const v = view({stage: "plan_failed", planner: "failed", text: "planning failed: no connectivity", robots: {euclid: robot({plan_id: null, stage: "no_plan", text: "no plan for this robot"})}});
    act(() => root.render(<GoalStatusView v={v} />));
    expect(host.textContent).toContain("planning failed: no connectivity");
  });
});

describe("GoalStatus", () => {
  it("follows hub pushes for its id", async () => {
    await act(async () => root.render(<GoalStatus id="g7" />));
    expect(host.textContent).toContain("waiting for the console");
    act(() => ws.inject({topic: "goal_track/g7", seq: 1, data: view({id: "g7", stage: "done", robots: {euclid: robot({stage: "done", text: "done: all 2 actions succeeded"})}})}));
    expect(host.querySelector('[data-testid="goal-status-euclid"]')!.textContent).toContain("done: all 2 actions succeeded");
  });
});
