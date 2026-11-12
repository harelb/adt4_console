import {afterEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {FleetChangeDetectionReset, mapText, robotResultText, type ResetPlan} from "../src/viewer/ChangeDetectionReset";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const PLAN: ResetPlan = {
  available: true,
  robots: [
    {robot: "euclid", state: "running", reachable: true, reason: null},
    {robot: "gauss", state: null, reachable: false, reason: "offline (fleetlink link down)"},
    {robot: "hamilton", state: "paused", reachable: true, reason: null},
  ],
  map: {writer: "/willow/scene_change_writer", running: true, keep_supported: true, reason: null},
};

const RESULT = {
  ok: true,
  keep_applied: true,
  map: {action: "keep", ok: true, message: "Kept 3 change detection change(s) in the map."},
  robots: [
    {robot: "euclid", ok: true, skipped: false, message: "reset requested"},
    {robot: "gauss", ok: false, skipped: true, error: "offline (fleetlink link down)"},
    {robot: "hamilton", ok: true, skipped: false, message: "reset requested"},
  ],
  base: {ok: true, message: "cleared euclid, hamilton"},
  scene_repair: {dropped: 1},
};

describe("fleet change detection reset", () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  const posts: {url: string; body: unknown}[] = [];
  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    vi.unstubAllGlobals();
    posts.length = 0;
  });
  const mount = async (plan: ResetPlan) => {
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        posts.push({url: String(url), body: JSON.parse(String(init.body))});
        return new Response(JSON.stringify(RESULT));
      }
      return new Response(JSON.stringify(plan));
    }));
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root!.render(<FleetChangeDetectionReset />));
    return host;
  };
  const q = (el: HTMLElement, id: string) => el.querySelector(`[data-testid=${id}]`) as HTMLElement;
  const click = async (el: HTMLElement) => act(async () => {
    el.click();
    await new Promise(r => setTimeout(r, 0));
  });

  it("asks first: which robots, which are skipped, the map keeps its applied changes", async () => {
    const el = await mount(PLAN);
    await click(q(el, "cd-reset-all"));
    expect(posts).toEqual([]);
    const dlg = q(el, "cd-reset-confirm");
    expect(dlg.textContent).toContain("euclid (running), hamilton (paused)");
    expect(q(el, "cd-reset-skipped").textContent).toContain("gauss — offline");
    expect(q(el, "cd-reset-map").textContent).toContain("KEPT");
    expect((q(el, "cd-reset-keep") as HTMLInputElement).checked).toBe(true);
    await click(q(el, "cd-reset-go"));
    expect(posts).toEqual([{url: "api/change_detection/reset_all", body: {keep_applied: true}}]);
    const res = q(el, "cd-reset-result").textContent!;
    expect(res).toContain("euclid: reset");
    expect(res).toContain("gauss: skipped (offline");
    expect(res).toContain("Kept 3");
    expect(q(el, "cd-reset-confirm")).toBeNull();
  });

  it("unchecking keep reverts; Cancel resets nothing", async () => {
    const el = await mount(PLAN);
    await click(q(el, "cd-reset-all"));
    await act(async () => q(el, "cd-reset-keep").click());
    expect(q(el, "cd-reset-map").textContent).toContain("REVERTED");
    await click(q(el, "cd-reset-go"));
    expect(posts).toEqual([{url: "api/change_detection/reset_all", body: {keep_applied: false}}]);

    posts.length = 0;
    await click(q(el, "cd-reset-all"));
    await click(Array.from(el.querySelectorAll("button")).find(b => b.textContent === "Cancel") as HTMLElement);
    expect(posts).toEqual([]);
  });

  it("a writer that cannot keep locks the choice to revert and says so", async () => {
    const el = await mount({...PLAN, map: {...PLAN.map, keep_supported: false}});
    await click(q(el, "cd-reset-all"));
    const keep = q(el, "cd-reset-keep") as HTMLInputElement;
    expect(keep.checked).toBe(false);
    expect(keep.disabled).toBe(true);
    expect(q(el, "cd-reset-confirm").textContent).toContain("cannot keep them");
    await click(q(el, "cd-reset-go"));
    expect(posts[0].body).toEqual({keep_applied: false});
  });

  it("texts", () => {
    expect(robotResultText({robot: "h", ok: false, skipped: false, error: "timeout"})).toBe("h: FAILED (timeout)");
    expect(mapText({...PLAN, map: {...PLAN.map, running: false, reason: "writer off"}}, true)).toContain("nothing to keep or revert");
  });
});
