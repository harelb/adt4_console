// Goal proposal cards: the offline-robot warning, and long goals / symbols wrap
// inside the card instead of running out of the chat bubble.
import {readFileSync} from "node:fs";
import {resolve} from "node:path";
import {afterEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {GoalCard, toChatItem, type ChatItem} from "../src/viewer/ChatPanel";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const LONG = "(and (visited-object-by euclid o29) (visited-object-by hamilton o31) (visited-object-by euclid o32) (visited-object-by hamilton o33))";

let root: Root | null = null;
let host: HTMLDivElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = host = null;
  vi.unstubAllGlobals();
});

function renderCard(o: Record<string, unknown>) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({engines: [], default: "fast_downward"}), {status: 200})));
  const item = toChatItem(o) as Extract<ChatItem, {type: "goal"}>;
  host = document.createElement("div");
  host.className = "chat-log";
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(<GoalCard it={item} robot={item.robot ?? null} src={null} onSend={() => undefined} />));
  return host;
}

describe("goal proposal card", () => {
  it("shows the offline-robot warning and sends only to the online team", () => {
    const el = renderCard({type: "goal", kind: "pddl", body: {goal: LONG}, robot: "euclid", team: ["euclid", "hamilton"], warning: "gauss is offline (not connected): its tasks wait until it connects"});
    expect(el.querySelector('[data-testid="goal-warning"]')?.textContent).toContain("gauss is offline");
    expect(el.querySelector('[data-testid="goal-send"]')?.textContent).toBe("Send to team (euclid, hamilton)");
  });

  it("no warning without one", () => {
    const el = renderCard({type: "goal", kind: "pddl", body: {goal: "(visited-object o2)"}, robot: "euclid", team: ["euclid", "hamilton"]});
    expect(el.querySelector('[data-testid="goal-warning"]')).toBeNull();
  });

  it("renders the goal in a wrapping goal-text block inside the card", () => {
    const el = renderCard({type: "goal", kind: "pddl", body: {goal: LONG}, robot: "euclid"});
    const pre = el.querySelector("pre.goal-text");
    expect(pre?.textContent).toBe(LONG);
    expect(pre?.closest(".msg")).not.toBeNull();
  });
});

describe("chat wrapping styles", () => {
  const css = readFileSync(resolve(__dirname, "../src/styles.css"), "utf8");
  const rule = (sel: string) => {
    const i = css.indexOf(`${sel} {`);
    expect(i, `missing rule ${sel}`).toBeGreaterThanOrEqual(0);
    return css.slice(i, css.indexOf("}", i));
  };

  it("pre blocks in chat bubbles wrap long single-line goals", () => {
    const r = rule(".chat-log .msg pre");
    expect(r).toContain("white-space: pre-wrap");
    expect(r).toContain("overflow-wrap: anywhere");
    expect(r).toContain("max-width: 100%");
  });

  it("bubbles, their flex children, chips and links can shrink and break long tokens", () => {
    expect(rule(".chat-log .msg")).toContain("min-width: 0");
    expect(rule(".chat-log .msg > *,\n.chat-log .msg .row > *")).toContain("min-width: 0");
    expect(rule(".chat-log .msg .chip:not(.chip-nowrap)")).toContain("white-space: normal");
    expect(rule(".chat-log .msg a")).toContain("overflow-wrap: anywhere");
  });
});
