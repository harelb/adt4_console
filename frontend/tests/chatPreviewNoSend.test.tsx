// The chat goal card's Preview plans in the console only: it never calls a send endpoint.
import {afterEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {GoalCard, toChatItem, type ChatItem} from "../src/viewer/ChatPanel";
import {DirectRouteButtons} from "../src/viewer/DirectRoute";
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

describe("goal card Preview", () => {
  it("only posts to preview_pddl, never to a robot's goal or path", async () => {
    const urls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (u: RequestInfo | URL) => {
        const url = String(u);
        urls.push(url);
        if (url.includes("preview_pddl"))
          return new Response(JSON.stringify({routes: {hamilton: [[0, 0], [1, 1]]}, driven_m: {hamilton: 1.4}, actions: [{robot: "hamilton", action: "goto-poi hamilton p1"}], robots: ["hamilton"], ground_s: 0.1, solve_s: 0.1, pois: 1}), {status: 200});
        return new Response(JSON.stringify({engines: [], default: "fast_downward"}), {status: 200});
      }),
    );
    useRobots.setState({state: {hamilton: {}}} as never);
    const onSend = vi.fn();
    const item = toChatItem({type: "goal", kind: "pddl", body: {goal: "(visited-object o2)"}, robot: "hamilton"}) as Extract<ChatItem, {type: "goal"}>;
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root!.render(<GoalCard it={item} robot="hamilton" src={null} onSend={onSend} />));
    const btn = [...host.querySelectorAll("button")].find(b => b.textContent === "Preview")!;
    await act(async () => {
      btn.click();
      await new Promise(r => setTimeout(r, 20));
    });
    expect(urls.some(u => u.includes("preview_pddl"))).toBe(true);
    expect(urls.filter(u => /\/goal\/|path\/direct|executor|execute/.test(u))).toEqual([]);
    expect(onSend).not.toHaveBeenCalled();
    expect(host.querySelector('[data-testid="goal-preview"]')).not.toBeNull();
  });

  it("keeps Preview and Send in one actions row under the goal, apart from the title, Send last", () => {
    const item = toChatItem({type: "goal", kind: "pddl", body: {goal: "(visited-object o2)"}, robot: "hamilton", team: ["euclid", "hamilton"]}) as Extract<ChatItem, {type: "goal"}>;
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root!.render(<GoalCard it={item} robot="hamilton" src={null} onSend={() => undefined} />));
    const card = host.querySelector(".msg-goal")!;
    const actions = card.querySelector('[data-testid="goal-actions"]')!;
    const kids = [...card.children];
    // the actions row comes after the goal text, so Preview's results (added
    // below it) never move a send button under the pointer
    expect(kids.indexOf(actions)).toBeGreaterThan(kids.indexOf(card.querySelector(".goal-text")!));
    // no button shares the title row
    expect(kids[0].querySelector("button")).toBeNull();
    const btns = [...actions.querySelectorAll("button")];
    expect(btns.map(b => b.dataset.testid)).toEqual(["goal-preview-btn", "goal-send"]);
    expect(btns[1].className).toContain("goal-send-btn");
  });
});

describe("direct route confirm", () => {
  it("puts the warning where the button was and Cancel before the send", () => {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root!.render(<DirectRouteButtons routes={{euclid: [[0, 0], [2, 0]]}} />));
    const row = host.querySelector(".direct-route-row")!;
    act(() => (row.querySelector("button") as HTMLButtonElement).click());
    expect(row.firstElementChild?.getAttribute("data-testid")).toBe("direct-route-warning-euclid");
    expect([...row.querySelectorAll("button")].map(b => b.textContent)).toEqual(["Cancel", "Send to euclid"]);
  });
});
