import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";

import {GoalEventToasts} from "../src/viewer/GoalEventToasts";
import {dismissGoalEvent, eventLine, onGoalEvent, TOAST_MS, useGoalEvents, type GoalEvent} from "../src/lib/goalEvents";
import {robotLine, type RobotTrackView} from "../src/lib/goalTrack";
import {useChat} from "../src/stores/chat";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

function ev(over: Partial<GoalEvent> = {}): GoalEvent {
  const t = new Date(2026, 10, 13, 18, 4, 12).getTime() / 1000;
  return {goal: "g1", robot: "hamilton", stage: "done", text: "done: all 2 actions succeeded", summary: "go to the tree", kind: "goto", t, ...over};
}

let root: Root;
let host: HTMLDivElement;
beforeEach(() => {
  useGoalEvents.setState({toasts: []});
  useChat.setState({items: []});
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.useRealTimers();
});

describe("goal events", () => {
  it("describes the event with robot, goal and time", () => {
    expect(eventLine(ev())).toBe("hamilton reached “go to the tree” at 18:04:12");
    expect(eventLine(ev({stage: "failed", robot: "gauss"}))).toBe("gauss failed “go to the tree” at 18:04:12");
    expect(eventLine(ev({stage: "preempted", summary: ""}))).toBe("hamilton stopped its goto goal at 18:04:12");
  });

  it("adds one toast and one chat line per goal, robot and stage", () => {
    onGoalEvent(ev());
    onGoalEvent(ev());
    expect(useGoalEvents.getState().toasts).toHaveLength(1);
    const lines = useChat.getState().items.filter(it => it.type === "goal_event");
    expect(lines).toHaveLength(1);
    onGoalEvent(ev({robot: "euclid"}));
    expect(useChat.getState().items.filter(it => it.type === "goal_event")).toHaveLength(2);
  });

  it("a reached toast goes away by itself; a failure stays until dismissed", () => {
    vi.useFakeTimers();
    onGoalEvent(ev());
    onGoalEvent(ev({goal: "g2", stage: "failed"}));
    expect(useGoalEvents.getState().toasts).toHaveLength(2);
    vi.advanceTimersByTime(TOAST_MS + 1);
    expect(useGoalEvents.getState().toasts.map(t => t.stage)).toEqual(["failed"]);
    dismissGoalEvent(useGoalEvents.getState().toasts[0].key);
    expect(useGoalEvents.getState().toasts).toEqual([]);
  });

  it("renders the toasts", () => {
    onGoalEvent(ev());
    act(() => root.render(<GoalEventToasts />));
    const toast = host.querySelector('[data-testid="goal-event-toast"]');
    expect(toast?.textContent).toContain("hamilton reached “go to the tree” at 18:04:12");
    expect(toast?.getAttribute("role")).toBe("status");
  });

  it("an ended robot step on the goal card says when", () => {
    const t = new Date(2026, 10, 13, 18, 4, 12).getTime() / 1000;
    const r = {robot: "hamilton", named: true, plan_id: "p1", actions: ["FOLLOW"], n: 1, done: 1, delivered: null, received: t - 5,
      received_how: "action result", paused: false, lease: null, lease_owner: "", stage: "done", text: "done: all 1 actions succeeded",
      events: [{t, text: "done: all 1 actions succeeded"}]} as RobotTrackView;
    expect(robotLine(r)).toBe("done: all 1 actions succeeded · at 18:04:12");
    expect(robotLine({...r, stage: "executing", text: "executing action 1/1 (FOLLOW)"})).toBe("received · executing action 1/1 (FOLLOW)");
  });
});
