import {afterEach, describe, expect, it, vi} from "vitest";
import {act, type ReactNode} from "react";
import {createRoot, type Root} from "react-dom/client";
import {HoldingEditor, holdingSummary, planStateText, PlannerStatePanel} from "../src/viewer/PlannerState";
import type {PlannerRobotState, PlannerStateSnapshot} from "../src/types";

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
  vi.unstubAllGlobals();
});
const flush = () =>
  act(async () => {
    await new Promise(r => setTimeout(r, 0));
  });
const byText = (t: string) => [...host!.querySelectorAll("button")].find(b => b.textContent?.trim() === t) as HTMLButtonElement;
const byLabel = (l: string) => host!.querySelector(`[aria-label="${l}"]`) as HTMLElement;
const click = (el: HTMLElement) => act(() => el.click());
function type(el: HTMLElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  act(() => {
    setter.call(el, value);
    el.dispatchEvent(new Event("input", {bubbles: true}));
  });
}

const robot = (over: Partial<PlannerRobotState>): PlannerRobotState => ({
  robot: "euclid",
  holding: [],
  position: null,
  in_plan: null,
  plan_state: null,
  constraints: [],
  last_error: null,
  ...over,
});

const snapshot = (): PlannerStateSnapshot => ({
  t: 1,
  ros: true,
  map: {source: "neo4j:base", error: null, version: 12, epoch: "abcdef0123", rebuilding: false, rebuild_failed: ""},
  planner: {map_version: 5, map_epoch: "abcdef0123", plan_robots: ["euclid"], plan_robots_t: 1, robot_plan_states_t: 1, goal_manager: true},
  robots: [
    robot({holding: ["O11"], in_plan: true, plan_state: {status: "running", done: 1, total: 4, conjuncts: ["(holding euclid o22)"], remaining: []}}),
    robot({robot: "hamilton", in_plan: false, constraints: [{predicate: "forbidden-poi", symbols: ["o3"]}], last_error: {t: 2, text: "no plan", source: "plan_status"}}),
  ],
  last_error: {t: 2, text: "no plan", source: "plan_status", robot: "hamilton"},
  corrections: [],
  holding_source: "Neo4j HOLDS",
});

describe("planner state helpers", () => {
  it("summarises holding and plan state", () => {
    const s = snapshot();
    expect(holdingSummary(s.robots)).toBe("euclid holds O11 · hamilton —");
    expect(planStateText(s.robots[0])).toBe("running 1/4");
    expect(planStateText(robot({in_plan: true}))).toBe("in plan");
    expect(planStateText(robot({}))).toBe("");
  });
});

describe("PlannerStatePanel", () => {
  it("shows what each robot holds, its plan state, constraints and the last error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(snapshot()), {status: 200, headers: {"Content-Type": "application/json"}})));
    render(<PlannerStatePanel />);
    await flush();
    expect(host!.querySelector('[data-testid="ps-holding-euclid"]')!.textContent).toContain("O11");
    expect(host!.querySelector('[data-testid="ps-holding-hamilton"]')!.textContent).toContain("nothing");
    expect(host!.textContent).toContain("running 1/4");
    expect(host!.textContent).toContain("(forbidden-poi o3)");
    expect(host!.textContent).toContain("Last plan error (hamilton)");
    expect(host!.textContent).toContain("map v12");
    expect(host!.textContent).toContain("(holding euclid o22)");
  });

  it("compact: a one-line summary in the chat", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(snapshot()), {status: 200, headers: {"Content-Type": "application/json"}})));
    render(<PlannerStatePanel compact />);
    await flush();
    const summary = host!.querySelector("summary")!;
    expect(summary.textContent).toContain("euclid holds O11");
    expect(summary.textContent).toContain("last plan error");
  });
});

describe("HoldingEditor", () => {
  it("asks before clearing holding, then posts it", async () => {
    const calls: [string, unknown][] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push([String(url), init?.body ? JSON.parse(String(init.body)) : undefined]);
      return new Response(JSON.stringify({ok: true, holding: {}}), {status: 200, headers: {"Content-Type": "application/json"}});
    }));
    const done = vi.fn();
    render(<HoldingEditor robot="euclid" holding={["O11"]} onDone={done} />);
    click(byText("Apply…"));
    expect(calls.length).toBe(0); // nothing until confirmed
    expect(byLabel("Confirm holding change").textContent).toContain("euclid holds nothing");
    click(byText("Confirm"));
    await flush();
    expect(calls[0][0]).toContain("api/plan/planner_state/holding");
    expect(calls[0][1]).toEqual({robot: "euclid", object: null, confirm: true});
    expect(done).toHaveBeenCalled();
  });

  it("sets holds X, and Cancel sends nothing", async () => {
    const calls: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
      calls.push(init?.body ? JSON.parse(String(init.body)) : undefined);
      return new Response(JSON.stringify({ok: true}), {status: 200, headers: {"Content-Type": "application/json"}});
    }));
    render(<HoldingEditor robot="euclid" holding={[]} />);
    type(byLabel("Object euclid holds"), "o22");
    click(byText("Apply…"));
    click(byText("Cancel"));
    expect(calls.length).toBe(0);
    click(byText("Apply…"));
    expect(byLabel("Confirm holding change").textContent).toContain("euclid holds O22");
    click(byText("Confirm"));
    await flush();
    expect(calls[0]).toEqual({robot: "euclid", object: "o22", confirm: true});
  });

  it("shows the server's refusal", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({detail: "O77 is not an object in the map"}), {status: 400, headers: {"Content-Type": "application/json"}})));
    render(<HoldingEditor robot="euclid" holding={[]} />);
    type(byLabel("Object euclid holds"), "O77");
    click(byText("Apply…"));
    click(byText("Confirm"));
    await flush();
    expect(host!.textContent).toContain("O77 is not an object in the map");
  });
});
