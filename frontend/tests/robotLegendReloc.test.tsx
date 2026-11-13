import {afterEach, describe, expect, it} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {RobotLegend} from "../src/viewer/RobotLegend";
import {useRobots} from "../src/stores/robots";
import {useViewer} from "../src/stores/viewer";

const useViewerFocus = () => (useViewer.getState() as unknown as {focus?: unknown}).focus;
let focus0: unknown;
const focusBefore = () => focus0;

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
          health: {
            nodes: [
              {nickname: "hamilton/roman_lc", status: "nominal", notes: "No loop closures found for robot 0, submap 54."},
              {nickname: "hamilton/hydra_multi", status: "nominal", notes: "44 / 51 valid interrobot loop closures"},
            ],
          },
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
    // fleetlink's ROMAN counter stayed 0 on both (agent callback starvation):
    // hamilton's hydra_multi row has the count; euclid's table did not reach the
    // base, but its map -> odom moved 0.5 m from identity
    expect(host.querySelector("[data-testid=align-hamilton]")?.textContent).toBe("relocalized · hydra 44 LCs");
    expect(host.querySelector("[data-testid=align-euclid]")?.textContent).toBe("aligned · no hydra count");
    expect(host.textContent).not.toMatch(/not relocalized/);
  });

  it("clicking a robot's localization opens the ROMAN / hydra counts and the last optimization", () => {
    const t0 = Date.now();
    useRobots.setState({
      link: {hamilton: {connected: true, frame_align: fa("hamilton", [-26.3, 6.97, 0.03, 1, 0, 0, 0]), reloc: {...NO_LC, prior_lcs: 3}}} as never,
      state: {
        hamilton: {
          frame_align: fa("hamilton", [-26.3, 6.97, 0.03, 1, 0, 0, 0]),
          reloc: {...NO_LC, prior_lcs: 3},
          health: {
            nodes: [
              {nickname: "hamilton/roman_lc", status: "nominal", notes: "3 loop closures"},
              {nickname: "hamilton/hydra_multi", status: "nominal", notes: "7 / 9 valid interrobot loop closures; last optimization took 0.412 s, 12.3 s ago"},
            ],
          },
        },
      } as never,
      stateAt: {hamilton: t0},
      plan: {},
    });
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    focus0 = useViewerFocus();
    act(() => root!.render(<RobotLegend />));
    const chip = host.querySelector("[data-testid=align-hamilton]") as HTMLElement;
    expect(chip.textContent).toBe("relocalized · hydra 7 LCs");
    expect(useViewerFocus()).toBe(focusBefore());
    act(() => chip.click());
    expect(host.querySelector("[data-testid=reloc-pop-hydra-hamilton]")?.textContent).toMatch(/7 accepted \/ 9 total.*relocalized at 5 accepted/);
    expect(host.querySelector("[data-testid=reloc-pop-roman-hamilton]")?.textContent).toMatch(/3 prior-map matches seen by fleetlink/);
    expect(host.querySelector("[data-testid=reloc-pop-opt-hamilton]")?.textContent).toMatch(/^1[2-3] s ago · took 412 ms$/);
    // the chip's click does not focus the robot
    expect(useViewerFocus()).toBe(focusBefore());
    act(() => (host!.querySelector("[aria-label=Close]") as HTMLButtonElement).click());
    expect(host.querySelector("[data-testid=reloc-pop-hamilton]")).toBeNull();
  });
});
