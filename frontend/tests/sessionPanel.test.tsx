import {afterEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {SessionPanel, evidenceText} from "../src/viewer/SessionPanel";
import {startBlockedReason, useLogbook, type LogbookState} from "../src/stores/logbook";
import {sendGoal} from "../src/viewer/PlanPanel";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const EV = {verdict: "relocalized", ok: true, reason: "relocalized (3 accepted)", hydra_accepted: 3, hydra_total: 5, roman_prior_lcs: 2, map_T_odom: [3, 4, 0, 1, 0, 0, 0], live: true};

function state(over: Partial<LogbookState> = {}): LogbookState {
  return {
    session: {session_id: "s20261111-120000_abcd", state: "open", t_start: 1, experiment: "exp1", mode: "relocalize", robots: ["euclid", "hamilton"], reloc_required: true},
    reloc: {
      euclid: {state: "relocalizing", t: 1, evidence: EV},
      hamilton: {state: "unlocalized", t: 1},
    },
    gate: {ok: false, reasons: ["euclid is relocalizing", "hamilton is unlocalized"], blocking: ["euclid", "hamilton"], session: true},
    mission: null,
    ...over,
  };
}

describe("session / relocalization / mission panel", () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  const posts: {url: string; body: unknown; headers?: HeadersInit}[] = [];
  let current: LogbookState = state();
  const mount = () => {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root!.render(<SessionPanel />));
    return host;
  };
  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    vi.unstubAllGlobals();
    posts.length = 0;
    useLogbook.setState({state: null, error: null});
  });
  const stub = () =>
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === "POST") {
          posts.push({url: String(url), body: JSON.parse(String(init.body ?? "{}")), headers: init.headers});
          return new Response(JSON.stringify(current));
        }
        return new Response(JSON.stringify(current));
      }),
    );
  const flush = () => act(async () => await new Promise(r => setTimeout(r, 0)));
  const q = (el: HTMLElement, id: string) => el.querySelector(`[data-testid=${id}]`) as HTMLButtonElement | null;

  it("shows the evidence and gates Start mission with the reason", async () => {
    current = state();
    stub();
    const el = mount();
    await flush();
    expect(el.textContent).toContain("s20261111-120000_abcd");
    expect(q(el, "reloc-evidence-euclid")!.textContent).toContain("hydra_multi 3 / 5 accepted");
    expect(q(el, "reloc-evidence-euclid")!.textContent).toContain("2 ROMAN prior matches");
    expect(q(el, "reloc-evidence-euclid")!.textContent).toContain("map→odom 5.0 m");
    const start = q(el, "mission-start")!;
    expect(start.disabled).toBe(true);
    expect(q(el, "mission-blocked")!.textContent).toContain("hamilton is unlocalized");
    // relocalizing: confirm / fail; unlocalized: start relocalizing
    expect(q(el, "reloc-confirm-euclid")).not.toBeNull();
    expect(q(el, "reloc-fail-euclid")).not.toBeNull();
    expect(q(el, "reloc-start-hamilton")).not.toBeNull();
    expect(q(el, "reloc-confirm-hamilton")).toBeNull();
  });

  it("Confirm / Start relocalizing / Override post to the logbook", async () => {
    current = state();
    stub();
    const el = mount();
    await flush();
    await act(async () => q(el, "reloc-confirm-euclid")!.click());
    await act(async () => q(el, "reloc-start-hamilton")!.click());
    expect(posts.map(p => p.url)).toEqual(["api/logbook/reloc/euclid/confirm", "api/logbook/reloc/hamilton/start"]);
    act(() => q(el, "reloc-override-hamilton")!.click());
    const ok = q(el, "reloc-override-confirm-hamilton")!;
    expect(ok.disabled).toBe(true); // a reason is required
    const input = q(el, "reloc-reason-hamilton") as unknown as HTMLInputElement;
    act(() => {
      const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      set.call(input, "radio down");
      input.dispatchEvent(new Event("input", {bubbles: true}));
    });
    await act(async () => q(el, "reloc-override-confirm-hamilton")!.click());
    expect(posts[2]).toMatchObject({url: "api/logbook/reloc/hamilton/override", body: {reason: "radio down"}});
  });

  it("Start mission once the gate is open, End mission while one is open", async () => {
    current = state({
      reloc: {euclid: {state: "relocalized", t: 1}, hamilton: {state: "overridden", t: 1, reason: "radio down"}},
      gate: {ok: true, reasons: [], blocking: [], session: true},
    });
    stub();
    const el = mount();
    await flush();
    expect(el.textContent).toContain("overridden: radio down");
    const start = q(el, "mission-start")!;
    expect(start.disabled).toBe(false);
    current = {...current, mission: {mission_id: "m20261111-121000_beef", state: "open", t_start: Date.now() / 1000, start_reason: "operator", goals: ["g1"], robots: []}};
    await act(async () => start.click());
    await flush();
    expect(posts[0].url).toBe("api/logbook/mission/start");
    expect(el.textContent).toContain("m20261111-121000_beef");
    expect(q(el, "mission-start")).toBeNull();
    current = {...current, mission: null};
    await act(async () => q(el, "mission-end")!.click());
    expect(posts[1].url).toBe("api/logbook/mission/end");
  });

  it("disabled reasons and evidence text", () => {
    expect(startBlockedReason(null)).toContain("not loaded");
    expect(startBlockedReason(state())).toContain("relocalize first");
    expect(startBlockedReason(state({gate: {ok: true, reasons: [], blocking: []}}))).toBe("");
    expect(evidenceText(undefined)).toBe("no evidence yet");
    expect(evidenceText({hydra_total: null, roman_prior_lcs: null, map_T_odom: null})).toContain("hydra_multi: no count");
  });

  it("sendGoal tells the backend who approved the send", async () => {
    stub();
    current = state();
    await sendGoal("euclid", "goto", {points: ["O(1)"]}, "operator");
    await sendGoal("euclid", "pddl", {goal: "x"});
    const h = (i: number) => new Headers(posts[i].headers).get("X-ADT4-Goal-Origin");
    expect(h(0)).toBe("operator");
    expect(h(1)).toBe("auto");
  });
});
