// "Reset map" in the mission chat: shows what a planning session added, asks once,
// then POSTs api/maps/clear_added (no map reload) and says what it removed.
import {afterEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {clearSummary, MapResetButton, neo4jTarget, pendingCount, type ClearResult} from "../src/viewer/MapResetButton";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLDivElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = host = null;
  vi.unstubAllGlobals();
});
async function render(el: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(el));
  return host;
}
const q = (h: HTMLElement, id: string) => h.querySelector(`[data-testid="${id}"]`) as HTMLButtonElement | null;
const click = async (b: HTMLElement | null) => act(async () => b!.click());

const status = {target: "pennfused", added_objects: 2, sam3_observations: 3, map_edits: 1};
const result: ClearResult = {
  ...status, added_objects: 0, sam3_observations: 0, map_edits: 0,
  removed: [{sym: "O700", class: "fire hydrant"}, {sym: "O704", class: "box"}], observations: 5, edits_undone: 1, undo_error: null,
};

function stubFetch() {
  const calls: {url: string; method: string; body?: string}[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({url: String(url), method: init?.method ?? "GET", body: init?.body as string | undefined});
      const body = init?.method === "POST" ? result : status;
      return new Response(JSON.stringify(body), {status: 200, headers: {"Content-Type": "application/json"}});
    }),
  );
  return calls;
}

describe("Reset map", () => {
  it("neo4jTarget and the counts", () => {
    expect(neo4jTarget("neo4j:pennfused")).toBe("pennfused");
    expect(neo4jTarget("file:pennfusedfile")).toBeNull();
    expect(pendingCount(status)).toBe(6);
    expect(pendingCount(null)).toBe(0);
    expect(clearSummary(result)).toBe("Map reset: removed 2 added objects, undid 1 map edit");
    expect(clearSummary({...result, removed: [], edits_undone: 0, observations: 0})).toBe("Map was already clean");
    expect(clearSummary({...result, undo_error: "node O9 is gone"})).toMatch(/could not undo every edit: node O9 is gone/);
  });

  it("not shown for a map that is not in Neo4j", async () => {
    stubFetch();
    const h = await render(<MapResetButton target={null} pollMs={0} />);
    expect(q(h, "map-reset")).toBeNull();
  });

  it("asks once, then clears and reports", async () => {
    const calls = stubFetch();
    const h = await render(<MapResetButton target="pennfused" pollMs={0} />);
    const open = q(h, "map-reset-open")!;
    expect(open.disabled).toBe(false);
    expect(open.textContent).toContain("Reset map (6)");
    expect(calls.every(c => c.method === "GET")).toBe(true);
    await click(open);
    expect(q(h, "map-reset-confirm")!.textContent).toContain("2 added objects, 1 map edit");
    await click(q(h, "map-reset-go"));
    const post = calls.find(c => c.method === "POST")!;
    expect(post.url).toContain("api/maps/clear_added");
    expect(JSON.parse(post.body!)).toEqual({target: "pennfused"});
    expect(q(h, "map-reset-note")!.textContent).toContain("removed 2 added objects, undid 1 map edit");
    expect(q(h, "map-reset-open")!.disabled).toBe(true); // nothing left to reset
  });

  it("Cancel leaves the map alone", async () => {
    const calls = stubFetch();
    const h = await render(<MapResetButton target="pennfused" pollMs={0} />);
    await click(q(h, "map-reset-open"));
    await click([...h.querySelectorAll("button")].find(b => b.textContent === "Cancel")!);
    expect(calls.some(c => c.method === "POST")).toBe(false);
    expect(q(h, "map-reset-open")).not.toBeNull();
  });
});
