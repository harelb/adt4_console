import {afterEach, describe, expect, it, vi} from "vitest";
import {act, type ReactNode} from "react";
import {createRoot, type Root} from "react-dom/client";
import type {SceneRecommendation, SceneRepairState} from "../src/types";

const handlers = new Map<string, (d: unknown) => void>();
vi.mock("../src/ws", () => ({
  ws: {
    on: (prefix: string, h: (d: unknown) => void) => {
      handlers.set(prefix, h);
      return () => handlers.delete(prefix);
    },
  },
}));

import {changeText, SCENE_REPAIR_TOPIC, SceneRepairPanel, sourceKind} from "../src/viewer/PlanRepair";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLDivElement | null = null;
function render(ui: ReactNode) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(ui));
  return host;
}
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  handlers.clear();
  vi.unstubAllGlobals();
});
const flush = () => act(async () => {
  await new Promise(r => setTimeout(r, 0));
});
const byText = (t: string) => [...host!.querySelectorAll("button")].find(b => b.textContent === t) as HTMLButtonElement;
const click = (el: HTMLElement) => act(() => el.click());

const REC: SceneRecommendation = {
  id: "sr1",
  robot: "hamilton",
  t: 1_700_000_000,
  source: "awcd/hamilton",
  origin: "perception",
  map_version: 12,
  reason: "new cone O43 is 3.1 m away, closer than goal O20 at 7.4 m",
  reasons: ["new cone O43 is 3.1 m away, closer than goal O20 at 7.4 m"],
  priority: "high",
  changes: [{kind: "added", symbol: "O43", label: "cone", old: null, new: [3.1, 0, 0], source: "awcd/hamilton", map_version: 12}],
  goal: {pddl: "(visited-object o20)", constraints: [], engine: null},
  proposed_goal: "(visited-object o43)",
  rewrites: [{from: "o20", to: "o43"}],
  status: "pending",
  result: null,
};

function state(over: Partial<SceneRepairState> = {}): SceneRepairState {
  return {
    map_version: 12,
    changes: [
      {id: 1, source: "awcd/hamilton", origin: "perception", map_version: 12, t: 1_700_000_000, changes: REC.changes},
      {id: 2, source: "console/harel", origin: "edit", map_version: 13, t: 1_700_000_010, summary: "edge P1 - P2 removed", changes: []},
    ],
    events: [],
    recommendations: [REC],
    pending: {hamilton: "sr1"},
    ...over,
  };
}

function stubFetch(initial: SceneRepairState) {
  const calls: [string, string, unknown][] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push([String(init?.method ?? "GET"), String(url), init?.body ? JSON.parse(String(init.body)) : undefined]);
    const body = String(url).includes("/confirm") || String(url).includes("/decline") ? {...REC, status: "confirmed"} : initial;
    return new Response(JSON.stringify(body), {status: 200, headers: {"Content-Type": "application/json"}});
  }));
  return calls;
}

describe("scene change helpers", () => {
  it("names sources and changes", () => {
    expect(sourceKind("awcd/hamilton")).toBe("awcd");
    expect(sourceKind("sget/chat")).toBe("sget");
    expect(sourceKind("console/harel")).toBe("console");
    expect(sourceKind("something")).toBe("other");
    expect(changeText({kind: "removed", symbol: "O20", label: "cone", old: null, new: null})).toBe("removed O20 cone");
  });
});

describe("SceneRepairPanel", () => {
  it("shows the pending recommendation with the original and proposed goal and the change feed", async () => {
    stubFetch(state());
    render(<SceneRepairPanel robot="hamilton" />);
    await flush();
    expect(host!.textContent).toContain("Replan recommended · hamilton");
    expect(host!.textContent).toContain("closer than goal O20");
    expect(host!.textContent).toContain("(visited-object o20)");
    expect(host!.textContent).toContain("(visited-object o43)");
    expect(byText("Replan with O43")).toBeTruthy();
    expect(byText("Replan same goal")).toBeTruthy();
    expect(byText("Dismiss")).toBeTruthy();
    const feed = host!.querySelector(".scene-feed")!;
    expect(feed.textContent).toContain("edge P1 - P2 removed");
    expect(feed.querySelector(".src-console")).toBeTruthy();
    expect(feed.querySelector(".src-awcd")).toBeTruthy();
    expect(feed.textContent).toContain("v13");
  });

  it("confirms the proposed goal, the original, or dismisses", async () => {
    const calls = stubFetch(state());
    render(<SceneRepairPanel robot="hamilton" />);
    await flush();
    click(byText("Replan with O43"));
    await flush();
    const posts = () => calls.filter(c => c[0] === "POST");
    expect(posts()[0][1]).toContain("api/plan/scene_repair/sr1/confirm");
    expect(posts()[0][2]).toEqual({variant: "proposed", held_id: ""});
    click(byText("Replan same goal"));
    await flush();
    expect(posts()[1][2]).toEqual({variant: "original", held_id: ""});
    act(() => (host!.querySelector('[aria-label="Resume robot on dismiss"]') as HTMLInputElement).click());
    click(byText("Dismiss"));
    await flush();
    expect(posts()[2][1]).toContain("api/plan/scene_repair/sr1/decline");
    expect(posts()[2][2]).toEqual({resume: true, held_id: ""});
  });

  it("hides another robot's recommendation and follows hub pushes", async () => {
    stubFetch(state({recommendations: [], pending: {}, changes: []}));
    render(<SceneRepairPanel robot="hamilton" />);
    await flush();
    expect(host!.textContent).toBe("");
    act(() => handlers.get(SCENE_REPAIR_TOPIC)!(state({recommendations: [{...REC, robot: "willow"}], pending: {willow: "sr1"}})));
    expect(host!.textContent).not.toContain("Replan recommended");
    expect(host!.textContent).toContain("Scene changes");
    // an edit without a rewrite offers only the same goal
    const edit: SceneRecommendation = {...REC, id: "sr2", origin: "edit", source: "sget/chat", rewrites: [], proposed_goal: REC.goal.pddl, reason: "map edited (sget/chat): O5 removed"};
    act(() => handlers.get(SCENE_REPAIR_TOPIC)!(state({recommendations: [edit], pending: {hamilton: "sr2"}})));
    expect(host!.textContent).toContain("map edited (sget/chat)");
    expect([...host!.querySelectorAll("button")].map(b => b.textContent)).toEqual(["Replan same goal", "Dismiss"]);
  });

  it("offers the goal the LLM proposed, next to the same goal", async () => {
    const llm: SceneRecommendation = {...REC, id: "sr3", llm: true, rewrites: [],
      goal: {pddl: "(and (visited-object o20) (visited-object o21))", constraints: [], engine: null},
      proposed_goal: "(and (visited-object o20) (visited-object o21) (visited-object o43))",
      reason: "New cone O43: added to all the cones."};
    const calls = stubFetch(state({recommendations: [llm], pending: {hamilton: "sr3"}}));
    render(<SceneRepairPanel robot="hamilton" />);
    await flush();
    expect(host!.textContent).toContain("(visited-object o43))");
    expect([...host!.querySelectorAll("button")].map(b => b.textContent)).toEqual(["Replan with new goal", "Replan same goal", "Dismiss"]);
    click(byText("Replan with new goal"));
    await flush();
    expect(calls.filter(c => c[0] === "POST")[0][2]).toEqual({variant: "proposed", held_id: ""});
  });

  it("says staged edits must be published before anything replans", async () => {
    stubFetch(state({recommendations: [], changes: [], pending: {}, staged: {src: "live", count: 2, summary: "delete O20", t: 1}}));
    render(<SceneRepairPanel robot="hamilton" />);
    await flush();
    const note = host!.querySelector(".scene-staged")!;
    expect(note.textContent).toContain("2 staged map edits not published yet");
    expect(note.textContent).toContain("Publish them");
  });
});

const HELD = {id: "q1", robots: ["hamilton"], reasons: ["scene changed: o20 removed"], reason: "scene changed: o20 removed",
              source: "awcd/hamilton", t: 1_700_000_000};
const heldRec = (over: Partial<SceneRecommendation> = {}): SceneRecommendation => ({
  ...REC, id: "sr2", origin: "planner", source: "awcd/hamilton", changes: [], rewrites: [],
  proposed_goal: REC.goal.pddl, reason: "scene changed: o20 removed", held: HELD, ...over,
});
const resumeBox = () => host!.querySelector<HTMLInputElement>('input[aria-label="Resume robot on dismiss"]')!;

describe("a replan the planner holds", () => {
  it("says the robots keep their plan, and Dismiss only declines", async () => {
    const calls = stubFetch(state({recommendations: [heldRec()], pending: {hamilton: "sr2"}}));
    render(<SceneRepairPanel robot="hamilton" />);
    await flush();
    expect(host!.textContent).toContain("The robots keep their current plan. Replan applies the repaired plan; Dismiss keeps the current plan.");
    expect(host!.textContent).not.toContain("applies to all");
    expect(host!.textContent).not.toContain("paused");
    expect(host!.querySelector('input[aria-label="Resume robot on dismiss"]')).toBeNull();
    click(byText("Dismiss"));
    await flush();
    expect(calls.find(([m, u]) => m === "POST" && u.includes("sr2/decline"))?.[2]).toEqual({resume: false, held_id: "q1"});
  });

  it("an ordinary card leaves the robot as it is on Dismiss", async () => {
    stubFetch(state());
    render(<SceneRepairPanel robot="hamilton" />);
    await flush();
    expect(host!.textContent).not.toContain("keep their current plan");
    expect(resumeBox().checked).toBe(false);
  });

  it("a card that became held shows the question's reasons and robots, and answers for that question", async () => {
    const held = {...HELD, id: "q9", robots: ["euclid", "hamilton"], reasons: ["scene changed: o20 removed", "scene changed: o21 moved"]};
    const calls = stubFetch(state({recommendations: [{...REC, held}]}));
    render(<SceneRepairPanel robot="hamilton" />);
    await flush();
    const items = [...host!.querySelectorAll(".scene-rec-held-reasons li")].map(li => li.textContent);
    expect(items).toEqual(["scene changed: o20 removed", "scene changed: o21 moved"]);
    expect(host!.textContent).toContain("euclid, hamilton");
    click(byText("Replan same goal"));
    await flush();
    expect(calls.find(([m, u]) => m === "POST" && u.includes("sr1/confirm"))?.[2]).toEqual({variant: "original", held_id: "q9"});
  });

  it("an ordinary card says it showed no question", async () => {
    const calls = stubFetch(state());
    render(<SceneRepairPanel robot="hamilton" />);
    await flush();
    click(byText("Dismiss"));
    await flush();
    expect(calls.find(([m, u]) => m === "POST" && u.includes("sr1/decline"))?.[2]).toEqual({resume: false, held_id: ""});
  });

  it("an answered card stays, waiting for the planner, and can send its answer again", async () => {
    const rec = heldRec({robot: "gauss", status: "declined", held: {...HELD, robots: ["gauss"]},
                         result: {ok: true, planner: "decline", question: "q1", waiting: true}});
    const calls = stubFetch(state({recommendations: [rec], pending: {}}));
    render(<SceneRepairPanel robot="gauss" />);
    await flush();
    expect(host!.textContent).toContain("Sent decline: waiting for the planner");
    expect(byText("Dismiss").disabled).toBe(false);
    expect(byText("Replan same goal").disabled).toBe(true);  // only its own answer again
    click(byText("Dismiss"));
    await flush();
    expect(calls.find(([m, u]) => m === "POST" && u.includes("sr2/decline"))?.[2]).toEqual({resume: false, held_id: "q1"});
  });

  it("a card waiting after Replan same goal disables the other actions", async () => {
    const rec = {...REC, status: "confirmed" as const, held: HELD,
                 result: {ok: true, planner: "confirm", question: "q1", waiting: true, variant: "original"}};
    stubFetch(state({recommendations: [rec], pending: {}}));
    render(<SceneRepairPanel robot="hamilton" />);
    await flush();
    expect(byText("Replan with O43").disabled).toBe(true);
    expect(byText("Dismiss").disabled).toBe(true);
    expect(byText("Replan same goal").disabled).toBe(false);
  });

  it("says the answer applies to every held robot", async () => {
    const held = {...HELD, robots: ["euclid", "gauss", "hamilton"]};
    stubFetch(state({recommendations: [heldRec({held})], pending: {hamilton: "sr2"}}));
    render(<SceneRepairPanel robot="hamilton" />);
    await flush();
    expect(host!.textContent).toContain("This answer applies to all of euclid, gauss, hamilton");
  });

  it("a held robot whose goal the console did not send says so", async () => {
    const rec = heldRec({goal: {pddl: "", constraints: [], engine: null, unknown: true}, proposed_goal: ""});
    stubFetch(state({recommendations: [rec], pending: {hamilton: "sr2"}}));
    render(<SceneRepairPanel robot="hamilton" />);
    await flush();
    expect(host!.textContent).toContain("the planner's (not sent from this console)");
    expect(byText("Replan same goal")).toBeTruthy();
  });
});
