// Operator fixes from the 2026-11-11 robot test: clear plan / path drawings
// (console only), the Chat tab without the pinned drive camera, and the grasp
// approval panel opened from a badge (Walk To confirmed).
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {hasDrawn, pathsCleared, pathsKey, planCleared, useClearedPaths} from "../src/stores/clearedPaths";
import {useRobots} from "../src/stores/robots";
import {usePlanPreview, type PddlPreview} from "../src/stores/planPreview";
import {useManipulation, pendingLabel} from "../src/stores/manipulation";
import {useGraspCamera} from "../src/stores/graspCamera";
import {useViewer} from "../src/stores/viewer";
import {ClearPathsButton, ClearRobotPaths} from "../src/viewer/ClearPaths";
import {cameraPlacement, loadCamOverChat, saveCamOverChat} from "../src/viewer/paneLayout";
import {GraspApprovalBadge, ManipulationApprovalHost} from "../src/viewer/ManipulationApproval";
import {GraspChip} from "../src/viewer/RobotLegend";
import type {RobotPaths} from "../src/lib/overlays";
import type {RobotPlan} from "../src/types";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

/** A Map-backed localStorage (a reload = a new store reading the same map). */
function mockStorage() {
  const m = new Map<string, string>();
  const ls = {
    getItem: (k: string) => (m.has(k) ? m.get(k)! : null),
    setItem: (k: string, v: string) => void m.set(k, String(v)),
    removeItem: (k: string) => void m.delete(k),
    clear: () => m.clear(),
    key: (i: number) => [...m.keys()][i] ?? null,
    get length() {
      return m.size;
    },
  };
  vi.stubGlobal("localStorage", ls);
  return m;
}

const plan = (id: string, robot = "spot1"): RobotPlan =>
  ({
    goals: [],
    results: [],
    llm: [],
    plan: {plan_id: id, robot_name: robot, t: 1, actions: [{type: "FOLLOW_PATH", path: [[0, 0, 0], [1, 0, 0]]}]},
  }) as unknown as RobotPlan;
const paths = (x: number, t = 1): RobotPaths => ({smooth: [[0, 0, 0], [x, 0, 0]], mlp: [], target: [x, 0, 0], frame: "map", t});

let root: Root | null = null;
let host: HTMLDivElement | null = null;
function render(el: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(el));
  return host;
}
afterEach(() => {
  if (root) act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
const button = (h: HTMLElement, text: string) => [...h.querySelectorAll("button")].find(b => b.textContent?.includes(text));

describe("clear paths (console only)", () => {
  let store: Map<string, string>;
  beforeEach(() => {
    store = mockStorage();
    useClearedPaths.getState().reload();
    useRobots.setState({plan: {}, paths: {}, state: {}, link: {}});
    usePlanPreview.setState({goal: null, preview: null});
  });

  it("clear hides a robot's plan until a NEW plan_id arrives", () => {
    useRobots.setState({plan: {spot1: plan("p1"), spot2: plan("q1", "spot2")}});
    expect(hasDrawn("spot1")).toBe(true);
    useClearedPaths.getState().clear("spot1");
    const c = useClearedPaths.getState();
    expect(planCleared(c, "spot1", useRobots.getState().plan.spot1)).toBe(true);
    expect(hasDrawn("spot1")).toBe(false);
    // the other robot is untouched
    expect(hasDrawn("spot2")).toBe(true);
    // the same plan re-latched (reconnect / restart): still hidden
    useRobots.setState(s => ({plan: {...s.plan, spot1: {...plan("p1"), results: []}}}));
    expect(hasDrawn("spot1")).toBe(false);
    // a new plan draws again
    useRobots.setState(s => ({plan: {...s.plan, spot1: plan("p2")}}));
    expect(hasDrawn("spot1")).toBe(true);
  });

  it("executor paths stay hidden while unchanged (new stamp, same geometry), shown again when they change", () => {
    useRobots.setState({paths: {spot1: paths(3)}});
    useClearedPaths.getState().clear("spot1");
    expect(pathsCleared(useClearedPaths.getState(), "spot1", paths(3, 99))).toBe(true);
    expect(pathsKey(paths(3, 1))).toBe(pathsKey(paths(3, 2)));
    expect(pathsCleared(useClearedPaths.getState(), "spot1", paths(5))).toBe(false);
    // the robot's own hash wins when it sends one
    expect(pathsKey({...paths(3), hash: "abc"})).toBe("h:abc");
  });

  it("persists the cleared ids per browser: a reload does not bring them back", () => {
    useRobots.setState({plan: {spot1: plan("p1")}, paths: {spot1: paths(2)}});
    useClearedPaths.getState().clearAll();
    expect(JSON.parse(store.get("adt4.clearedPaths")!)).toMatchObject({plans: {spot1: "p1"}});
    // "reload": the store re-reads storage
    useClearedPaths.setState({plans: {}, paths: {}});
    expect(hasDrawn("spot1")).toBe(true);
    useClearedPaths.getState().reload();
    expect(hasDrawn("spot1")).toBe(false);
    // restore brings it back, and that is remembered too
    useClearedPaths.getState().restore("spot1");
    useClearedPaths.getState().reload();
    expect(hasDrawn("spot1")).toBe(true);
  });

  it("blocked storage does not break clearing", () => {
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    });
    useClearedPaths.getState().reload();
    useRobots.setState({plan: {spot1: plan("p1")}});
    useClearedPaths.getState().clear("spot1");
    expect(hasDrawn("spot1")).toBe(false);
  });

  it("the toolbar button clears every robot and the plan preview; nothing is sent", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    useRobots.setState({plan: {spot1: plan("p1"), spot2: plan("q1", "spot2")}, paths: {spot2: paths(1)}});
    usePlanPreview.setState({goal: "(visited a)", preview: {routes: {spot1: [[0, 0], [1, 1]]}} as unknown as PddlPreview});
    const h = render(<ClearPathsButton />);
    const b = h.querySelector<HTMLButtonElement>('[data-testid="clear-paths"]')!;
    expect(b.disabled).toBe(false);
    act(() => b.click());
    expect(hasDrawn("spot1")).toBe(false);
    expect(hasDrawn("spot2")).toBe(false);
    expect(usePlanPreview.getState().preview).toBeNull();
    expect(b.disabled).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
    // a new plan re-enables it
    act(() => useRobots.setState(s => ({plan: {...s.plan, spot1: plan("p2")}})));
    expect(b.disabled).toBe(false);
  });

  it("the Plan tab clears one robot and can show it again", () => {
    useRobots.setState({plan: {spot1: plan("p1"), spot2: plan("q1", "spot2")}});
    const h = render(<ClearRobotPaths robot="spot1" />);
    act(() => button(h, "Clear path")!.click());
    expect(hasDrawn("spot1")).toBe(false);
    expect(hasDrawn("spot2")).toBe(true);
    act(() => button(h, "Show path")!.click());
    expect(hasDrawn("spot1")).toBe(true);
  });
});

describe("chat tab without the pinned camera", () => {
  const base = {drive: true, isPhone: false, rightOpen: true, cameraFloats: false, camOverChat: false, grasping: false};

  it("drive layout: Chat folds the camera; Teleop keeps it pinned", () => {
    expect(cameraPlacement({...base, dockTab: "chat"})).toEqual({pinned: false, folded: true, pip: false});
    expect(cameraPlacement({...base, dockTab: "teleop"})).toEqual({pinned: true, folded: false, pip: false});
    // no drive layout: no pinned camera anywhere
    expect(cameraPlacement({...base, drive: false, dockTab: "chat"})).toEqual({pinned: false, folded: false, pip: false});
  });

  it("the operator can re-show it over Chat; a grasp always shows it", () => {
    expect(cameraPlacement({...base, dockTab: "chat", camOverChat: true}).pinned).toBe(true);
    expect(cameraPlacement({...base, dockTab: "chat", grasping: true})).toMatchObject({pinned: true, folded: false});
  });

  it("the re-show choice is remembered per browser (default: folded)", () => {
    const store = mockStorage();
    expect(loadCamOverChat()).toBe(false);
    saveCamOverChat(true);
    expect(store.get("adt4.viewer.cameraOverChat")).toBe("on");
    expect(loadCamOverChat()).toBe(true);
    saveCamOverChat(false);
    expect(loadCamOverChat()).toBe(false);
  });
});

describe("grasp approval opens from a badge", () => {
  const request = {
    id: "g1",
    robot: "spot1",
    received_at: Date.now() / 1000,
    expires_at: Date.now() / 1000 + 300,
    images: [{index: 0, name: "hand", width: 64, height: 48, url: "/img/0.png"}],
    selection: {image_index: 0, x: 3, y: 4},
  };
  let responded: unknown[];
  beforeEach(() => {
    useManipulation.setState({pending: [], openId: null, badges: 0});
    useGraspCamera.setState({enabled: true, session: null, last: null});
    useRobots.setState({plan: {}, paths: {}, state: {}, link: {}});
    useViewer.getState().setFocus("all");
    responded = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (url.endsWith("/respond")) responded.push(JSON.parse(String(init?.body)));
        const body = url.endsWith("pending")
          ? {available: true, error: null, requests: responded.length ? [] : [request]}
          : url.includes("/cameras")
            ? {cameras: ["frontleft", "hand"]}
            : {ok: true, status: "published", robot: "spot1", request_id: "g1"};
        return new Response(JSON.stringify(body), {status: 200, headers: {"Content-Type": "application/json"}});
      }),
    );
  });
  const flush = () =>
    act(async () => {
      await new Promise(r => setTimeout(r, 0));
    });

  it("a request shows a pulsing button, not the panel; clicking opens it; approve is unchanged and starts the hand camera", async () => {
    const h = render(<ManipulationApprovalHost />);
    await flush();
    expect(h.querySelector('[role="dialog"]')).toBeNull();
    const open = h.querySelector<HTMLButtonElement>('[data-testid="grasp-approval-open"]')!;
    expect(open.textContent).toBe("Grasp approval: spot1 — open");
    expect(open.className).toContain("is-pulsing");
    expect(useManipulation.getState().pending).toEqual([{id: "g1", robot: "spot1", simulated: undefined}]);
    act(() => open.click());
    expect(h.querySelector('[role="dialog"]')).not.toBeNull();
    // closing keeps it pending (the robot waits): the button is back
    act(() => button(h, "Close without response")!.click());
    expect(h.querySelector('[role="dialog"]')).toBeNull();
    expect(h.querySelector('[data-testid="grasp-approval-open"]')).not.toBeNull();
    expect(responded).toEqual([]);
    act(() => h.querySelector<HTMLButtonElement>('[data-testid="grasp-approval-open"]')!.click());
    act(() => h.querySelector("img")!.dispatchEvent(new Event("load", {bubbles: true})));
    await act(async () => button(h, "Approve selected")!.click());
    await flush();
    expect(responded).toEqual([{approve: true, image_index: 0, image_x: 3, image_y: 4}]);
    expect(useGraspCamera.getState().session).toMatchObject({robot: "spot1", requestId: "g1"});
    expect(useManipulation.getState().openId).toBeNull();
  });

  it("the robot's legend chip and the viewer toolbar badge open it; the floating button steps aside in the viewer", async () => {
    const h = render(
      <>
        <ManipulationApprovalHost />
        <GraspApprovalBadge />
        <span role="button" onClick={() => useViewer.getState().setFocus("spot1")}>
          spot1 <GraspChip robot="spot1" />
        </span>
      </>,
    );
    await flush();
    // only the toolbar badge (the global floating button hides while the viewer shows one)
    expect(h.querySelectorAll('[data-testid="grasp-approval-open"]').length).toBe(1);
    const chip = h.querySelector<HTMLButtonElement>('[data-testid="grasp-chip-spot1"]')!;
    expect(chip.textContent).toBe("Grasp approval");
    act(() => chip.click());
    expect(h.querySelector('[role="dialog"]')).not.toBeNull();
    // the chip click did not also focus the robot
    expect(useViewer.getState().focus).not.toBe("spot1");
    act(() => button(h, "Close without response")!.click());
    act(() => h.querySelector<HTMLButtonElement>('[data-testid="grasp-approval-open"]')!.click());
    expect(h.querySelector('[role="dialog"]')).not.toBeNull();
  });

  it("walk-to needs the confirmation tick before anything is sent", async () => {
    const h = render(<ManipulationApprovalHost />);
    await flush();
    act(() => h.querySelector<HTMLButtonElement>('[data-testid="grasp-approval-open"]')!.click());
    act(() => h.querySelector("img")!.dispatchEvent(new Event("load", {bubbles: true})));
    await act(async () => button(h, "Walk to selected point")!.click());
    expect(responded).toEqual([]);
    expect(button(h, "Send walk-to")!.disabled).toBe(true);
    act(() => h.querySelector<HTMLInputElement>('[data-testid="walk-confirm"] input[type="checkbox"]')!.click());
    await act(async () => button(h, "Send walk-to")!.click());
    await flush();
    expect(responded).toEqual([{approve: false, image_index: 0, image_x: 3, image_y: 4, walk_to: true}]);
    expect(useGraspCamera.getState().session).toBeNull();
  });

  it("labels several pending requests", () => {
    expect(
      pendingLabel([
        {id: "a", robot: "spot1"},
        {id: "b", robot: "spot2"},
      ]),
    ).toBe("Grasp approvals (2): spot1, spot2 — open");
  });
});
