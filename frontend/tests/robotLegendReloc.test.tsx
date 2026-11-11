import {afterEach, describe, expect, it} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {RobotLegend} from "../src/viewer/RobotLegend";
import {useRobots} from "../src/stores/robots";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

// 2026-11-11 robot test: team = euclid + hamilton relocalizing against
// Nov11_Building45_v2 (recorded by euclid); gauss is in the topology but not running.
const fa = (r: string, T: number[]) => ({parent: "map", child: `${r}/odom`, ok: true, T, stamp: 1, age_s: 0.5});
const NO_LC = {prior_lcs: 0, self_lcs: 0, best_assoc: 0, last_lc_age_s: null, lcs: [], jumps: 0, last_jump: null, recent_jumps: [], total: {dxy: 0, dyaw: 0}};

describe("robot legend relocalization status", () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
  });

  it("shows only live team robots, and never names another robot in the status", () => {
    useRobots.setState({
      link: {euclid: {connected: true, frame_align: fa("euclid", [0.04, -0.53, 0.2, 1, 0, 0, 0]), reloc: NO_LC}, hamilton: {connected: true, frame_align: fa("hamilton", [-26.3, 6.97, 0.03, 1, 0, 0, 0]), reloc: NO_LC}} as never,
      state: {
        hamilton: {
          frame_align: fa("hamilton", [-26.3, 6.97, 0.03, 1, 0, 0, 0]),
          reloc: NO_LC,
          health: {nodes: [{nickname: "hamilton/roman_lc", status: "nominal", notes: "No loop closures found for robot 0, submap 54."}]},
        },
      } as never,
      stateAt: {hamilton: Date.now()},
      plan: {},
    });
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root!.render(<RobotLegend />));
    const chips = [...host.querySelectorAll(".robot-chip")].map(c => c.textContent ?? "");
    expect(chips.length).toBe(2);
    expect(host.textContent).not.toMatch(/gauss|guess/i);
    expect(host.querySelector("[data-testid=align-euclid]")?.textContent).toBe("not relocalized · no prior-map match yet");
    expect(host.querySelector("[data-testid=align-hamilton]")?.textContent).toBe("not relocalized · matching prior map…");
  });
});
