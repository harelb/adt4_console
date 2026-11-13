import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {GRASP_CAM, GRASP_TIMEOUT_MS, GRASP_TUNE, isGraspResult, loadGraspEnabled, useGraspCamera} from "../src/stores/graspCamera";
import {useRobots} from "../src/stores/robots";
import {useViewer} from "../src/stores/viewer";
import {useFleet} from "../src/stores/fleet";
import {useManipulation} from "../src/stores/manipulation";
import type {ActionResult, RobotPlan} from "../src/types";

vi.mock("../src/viewer/VideoView", async importOriginal => ({
  ...(await importOriginal<typeof import("../src/viewer/VideoView")>()),
  VideoView: ({robot, cam, tune}: {robot: string; cam: string; tune?: unknown}) => <div data-testid="video" data-robot={robot} data-cam={cam} data-tune={tune ? JSON.stringify(tune) : ""} />,
}));

const {CameraPanel} = await import("../src/viewer/CameraPanel");
const {ManipulationApprovalHost} = await import("../src/viewer/ManipulationApproval");

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const SPOT_CAMS = ["frontleft", "frontright", "hand"];
const pick = (status: string, extra: Partial<ActionResult> = {}): ActionResult => ({plan_id: "p1", action_index: 0, action_type: "PICK", status, t: 100, ...extra});
const plan = (results: ActionResult[], lease?: RobotPlan["lease"]) => ({goals: [], plan: null, results, llm: [], lease}) as RobotPlan;
const approve = (robot = "spot1", requestId = "r1", cams: string[] | null = SPOT_CAMS, now = 1000) =>
  useGraspCamera.getState().answered({robot, requestId, kind: "approve", cams}, now);

function reset() {
  useGraspCamera.setState({enabled: true, session: null, last: null});
  useManipulation.setState({pending: [], openId: null, badges: 0});
  useRobots.setState({state: {}, link: {}, plan: {}, teleop: {}});
  useViewer.getState().setFocus("all");
}

describe("grasp camera state machine", () => {
  beforeEach(reset);

  it("approve with a hand camera starts a session and focuses the robot; a PICK result ends it and restores focus", () => {
    useViewer.getState().setFocus("other");
    approve();
    const s = useGraspCamera.getState().session;
    expect(s?.robot).toBe("spot1");
    expect(s?.deadline).toBe(1000 + GRASP_TIMEOUT_MS);
    expect(useViewer.getState().focus).toBe("spot1");
    useGraspCamera.getState().planUpdate("spot1", plan([pick("SUCCESS")]));
    expect(useGraspCamera.getState().session).toBeNull();
    expect(useGraspCamera.getState().last?.reason).toBe("result");
    expect(useViewer.getState().focus).toBe("other");
  });

  it("only a new, finished PICK / DRAG result ends the grasp", () => {
    const old = pick("FAILED", {plan_id: "old"});
    useRobots.setState({plan: {spot1: plan([old])}});
    approve();
    const st = useGraspCamera.getState();
    st.planUpdate("spot1", plan([old])); // already there at approval
    st.planUpdate("spot1", plan([old, pick("SUCCESS", {action_type: "GOTO"})]));
    st.planUpdate("spot1", plan([old, pick("STARTED")]));
    st.planUpdate("other", plan([pick("SUCCESS")]));
    expect(useGraspCamera.getState().session).not.toBeNull();
    useGraspCamera.getState().planUpdate("spot1", plan([old, pick("PREEMPTED", {action_type: "DRAG"})]));
    expect(useGraspCamera.getState().last?.reason).toBe("result");
    expect(isGraspResult(pick("FAILED"))).toBe(true);
    expect(isGraspResult(pick("SUCCESS", {action_type: "PLACE"}))).toBe(false);
  });

  it("a new manipulation request from the robot ends it; the answered one does not", () => {
    approve();
    useGraspCamera.getState().requestSeen("spot1", "r1");
    useGraspCamera.getState().requestSeen("other", "x");
    expect(useGraspCamera.getState().session).not.toBeNull();
    useGraspCamera.getState().requestSeen("spot1", "r2");
    expect(useGraspCamera.getState().session).toBeNull();
    expect(useGraspCamera.getState().last?.reason).toBe("new_request");
  });

  it("times out after GRASP_TIMEOUT_MS", () => {
    approve();
    useGraspCamera.getState().tick(1000 + GRASP_TIMEOUT_MS - 1);
    expect(useGraspCamera.getState().session).not.toBeNull();
    useGraspCamera.getState().tick(1000 + GRASP_TIMEOUT_MS);
    expect(useGraspCamera.getState().last?.reason).toBe("timeout");
  });

  it("a robot without a hand camera (or an unknown camera list) is left alone", () => {
    approve("husky", "r1", ["front"]);
    approve("husky", "r2", null);
    expect(useGraspCamera.getState().session).toBeNull();
    expect(useViewer.getState().focus).toBe("all");
  });

  it("Walk To keeps the current camera and ends a running session", () => {
    useGraspCamera.getState().answered({robot: "spot1", requestId: "r1", kind: "walk", cams: SPOT_CAMS});
    expect(useGraspCamera.getState().session).toBeNull();
    approve();
    useGraspCamera.getState().answered({robot: "spot1", requestId: "r2", kind: "walk", cams: SPOT_CAMS});
    expect(useGraspCamera.getState().last?.reason).toBe("walk_to");
  });

  it("losing the lease during the grasp ends it", () => {
    useRobots.setState({plan: {spot1: plan([], {known: true, owned: true, owner: "console"})}});
    approve();
    useGraspCamera.getState().planUpdate("spot1", plan([], {known: true, owned: true, owner: "console"}));
    expect(useGraspCamera.getState().session).not.toBeNull();
    useGraspCamera.getState().planUpdate("spot1", plan([], {known: true, owned: false, owner: "tablet"}));
    expect(useGraspCamera.getState().last?.reason).toBe("lease");
  });

  it("the setting is remembered per browser and off means no switch", () => {
    useGraspCamera.getState().setEnabled(false);
    expect(localStorage.getItem("adt4.graspHandCamera")).toBe("off");
    expect(loadGraspEnabled()).toBe(false);
    approve();
    expect(useGraspCamera.getState().session).toBeNull();
    useGraspCamera.getState().setEnabled(true);
    expect(loadGraspEnabled()).toBe(true);
  });
});

describe("Camera panel during a grasp", () => {
  let root: Root | null = null;
  let container: HTMLDivElement;
  beforeEach(() => {
    reset();
    useFleet.setState({machines: {network: "test", machines: [{name: "spot1", role: "robot", ip: "", user: ""}]}} as never);
    useRobots.setState({state: {spot1: {platform: "spot"}} as never});
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (String(url).includes("/cameras")) return new Response(JSON.stringify({cameras: SPOT_CAMS}), {headers: {"Content-Type": "application/json"}});
        return new Response("no", {status: 503});
      }),
    );
  });
  afterEach(() => {
    if (root) act(() => root?.unmount());
    root = null;
    container?.remove();
    vi.unstubAllGlobals();
  });

  async function render() {
    useViewer.getState().setFocus("spot1");
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    await act(async () => root!.render(<CameraPanel />));
    await act(async () => {
      await new Promise(r => setTimeout(r, 0));
    });
  }
  const video = () => container.querySelector<HTMLElement>("[data-testid=video]");
  const badge = () => container.querySelector("[data-testid=grasp-cam-badge]");
  const select = () => container.querySelector<HTMLSelectElement>("select[aria-label='Camera stream']")!;

  it("approve -> hand video with the grasp stream settings; PICK result -> the previous still view", async () => {
    await render();
    expect(video()).toBeNull(); // operator on stills of frontleft
    expect(select().value).toBe("frontleft");
    await act(async () => approve());
    expect(video()?.dataset.cam).toBe(GRASP_CAM);
    expect(JSON.parse(video()!.dataset.tune!)).toEqual(GRASP_TUNE);
    expect(badge()?.textContent).toContain("hand camera · grasp in progress");
    await act(async () => useGraspCamera.getState().planUpdate("spot1", plan([pick("SUCCESS")])));
    expect(video()).toBeNull();
    expect(badge()).toBeNull();
    expect(select().value).toBe("frontleft");
  });

  it("the operator picking a camera cancels the override and keeps the choice", async () => {
    await render();
    await act(async () => approve());
    await act(async () => {
      select().value = "frontright";
      select().dispatchEvent(new Event("change", {bubbles: true}));
    });
    expect(useGraspCamera.getState().last?.reason).toBe("manual");
    expect(video()?.dataset.cam).toBe("frontright"); // stays in video, the operator's camera
    expect(video()?.dataset.tune).toBe("");
    expect(badge()).toBeNull();
  });

  it("back restores the previous view at once", async () => {
    await render();
    await act(async () => approve());
    const back = [...container.querySelectorAll("button")].find(b => b.textContent === "back")!;
    await act(async () => back.click());
    expect(useGraspCamera.getState().last?.reason).toBe("back");
    expect(video()).toBeNull();
    expect(select().value).toBe("frontleft");
  });
});

describe("approval dialog -> grasp camera", () => {
  let root: Root | null = null;
  let container: HTMLDivElement;
  beforeEach(reset);
  afterEach(() => {
    if (root) act(() => root?.unmount());
    root = null;
    container?.remove();
    vi.unstubAllGlobals();
  });

  async function answer(label: string, cams: string[]) {
    const request = {id: "g1", robot: "spot1", received_at: Date.now() / 1000, expires_at: Date.now() / 1000 + 300,
      images: [{index: 0, name: "hand", width: 64, height: 48, url: "/img/0.png"}], selection: {image_index: 0, x: 3, y: 4}};
    let answered = false;
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.endsWith("/respond")) answered = true;
      const body = url.endsWith("pending") ? {available: true, error: null, requests: answered ? [] : [request]}
        : url.includes("/cameras") ? {cameras: cams}
        : {ok: true, status: "published", robot: "spot1", request_id: "g1"};
      return new Response(JSON.stringify(body), {status: 200, headers: {"Content-Type": "application/json"}});
    }));
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    await act(async () => root!.render(<ManipulationApprovalHost />));
    await act(async () => {
      await new Promise(r => setTimeout(r, 0));
    });
    // the request waits behind a button; the operator opens it
    expect(container.querySelector("img")).toBeNull();
    act(() => container.querySelector<HTMLButtonElement>('[data-testid="grasp-approval-open"]')!.click());
    const img = container.querySelector("img")!;
    act(() => img.dispatchEvent(new Event("load", {bubbles: true})));
    expect(container.textContent).toContain("Auto hand camera during grasp");
    const btn = [...container.querySelectorAll("button")].find(b => b.textContent?.includes(label))!;
    await act(async () => btn.click());
    if (label.startsWith("Walk to")) {
      // Walk To asks for a confirmation first
      act(() => container.querySelector<HTMLInputElement>('[data-testid="walk-confirm"] input[type="checkbox"]')!.click());
      const send = [...container.querySelectorAll("button")].find(b => b.textContent?.includes("Send walk-to"))!;
      await act(async () => send.click());
    }
    await act(async () => {
      await new Promise(r => setTimeout(r, 0));
    });
  }

  it("approving a grasp on a robot with a hand camera starts the hand view", async () => {
    await answer("Approve selected", SPOT_CAMS);
    expect(useGraspCamera.getState().session).toMatchObject({robot: "spot1", requestId: "g1"});
  });

  it("no hand camera: nothing switches", async () => {
    await answer("Approve selected", ["front"]);
    expect(useGraspCamera.getState().session).toBeNull();
  });

  it("Walk To does not switch", async () => {
    await answer("Walk to selected", SPOT_CAMS);
    expect(useGraspCamera.getState().session).toBeNull();
  });
});
