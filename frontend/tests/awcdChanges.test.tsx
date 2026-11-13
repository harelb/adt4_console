import {afterEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {AWCD_COLORS, boxEdges, changeColor, changeLabel, countsText, labelAt, shortId, type AwcdChange, type AwcdState} from "../src/lib/awcd";
import {AwcdChangesPanel} from "../src/viewer/AwcdChanges";
import {useAwcd} from "../src/stores/awcd";
import {useViewer} from "../src/stores/viewer";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const change = (p: Partial<AwcdChange>): AwcdChange => ({
  key: "r42",
  kind: "removed",
  id: 42,
  label: "cone",
  center: [3, 4, 0.5],
  dims: [1, 2, 1],
  quat: [0, 0, 0, 1],
  stamp: 1700000000,
  change_confidence: 0.9,
  confidence: 0.8,
  frames: 7,
  robots: ["euclid", "hamilton"],
  ...p,
});

const STATE: AwcdState = {
  available: true,
  topic: "/willow/awcd_base_station/reconciled_changes",
  t: 1,
  frame_id: "map",
  changes: [change({}), change({key: "a99", kind: "added", id: 2 ** 40 + 5, short_id: 5, label: "bag", center: [-1, 0, 0], robots: ["hamilton"]})],
  counts: {added: 1, removed: 1},
  robots: {hamilton: {t: 1, added: 1, removed: 1}},
};

describe("awcd formatting", () => {
  it("colours match awcd_visualizer (added green, removed red)", () => {
    expect(changeColor("added")).toBe(AWCD_COLORS.added);
    expect(AWCD_COLORS).toEqual({added: "#00ff00", removed: "#ff0000"});
  });

  it("labels like RViz, with the track id of an added object", () => {
    expect(changeLabel(change({}))).toBe("euclid, hamilton | removed 42 | cone | cc=0.90 n=7");
    expect(shortId(STATE.changes[1])).toBe("5");
    expect(changeLabel(change({robots: [], label: ""}))).toContain("? | removed 42 | unknown");
    expect(countsText(STATE)).toBe("1 added · 1 removed");
    expect(countsText(null)).toBe("no changes");
    expect(labelAt(change({}))).toEqual([3, 4, 1.4]);
  });

  it("box edges: 24 points spanning the box, rotated by the quaternion", () => {
    const pts = boxEdges([3, 4, 0.5], [1, 2, 1], [0, 0, 0, 1]);
    expect(pts).toHaveLength(24);
    const xs = pts.map(p => p[0]);
    const ys = pts.map(p => p[1]);
    expect([Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)]).toEqual([2.5, 3.5, 3, 5]);
    // 90 deg about z: the 1 x 2 footprint becomes 2 x 1
    const s = Math.SQRT1_2;
    const r = boxEdges([0, 0, 0], [1, 2, 1], [0, 0, s, s]);
    expect(Math.max(...r.map(p => p[0]))).toBeCloseTo(1);
    expect(Math.max(...r.map(p => p[1]))).toBeCloseTo(0.5);
  });
});

describe("awcd change list", () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  const posts: {url: string; body: unknown}[] = [];
  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    vi.unstubAllGlobals();
    posts.length = 0;
    useAwcd.setState({state: null, focus: null});
  });
  const mount = async (st: AwcdState) => {
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        posts.push({url: String(url), body: JSON.parse(String(init.body))});
        return new Response(JSON.stringify({ok: true, message: "cleared 2 robots"}));
      }
      return new Response(JSON.stringify(st));
    }));
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root!.render(<AwcdChangesPanel />));
    await act(async () => await new Promise(r => setTimeout(r, 0)));
    return host;
  };

  it("lists the changes; a click focuses and flies to one", async () => {
    const fly = vi.fn();
    useViewer.setState({fly} as never);
    const el = await mount(STATE);
    expect(el.textContent).toContain("1 added · 1 removed");
    const rows = el.querySelectorAll("[data-testid=awcd-list] button");
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain("removed");
    expect(rows[1].textContent).toContain("#5");
    act(() => (rows[1] as HTMLButtonElement).click());
    expect(useAwcd.getState().focus).toBe("a99");
    expect(fly).toHaveBeenCalledWith([-1, 0, 0]);
  });

  it("the panel offers the fleet-wide reset, not a base-station-only clear", async () => {
    const el = await mount(STATE);
    expect(el.querySelector("[data-testid=cd-reset-all]")).not.toBeNull();
    expect(Array.from(el.querySelectorAll("button")).some(b => b.textContent === "Clear at base station…")).toBe(false);
  });

  it("hides the controls when AWCD is unavailable", async () => {
    const el = await mount({...STATE, available: false, error: "khronos_msgs/msg/AwcdChanges unavailable", changes: []});
    expect(el.querySelector("[data-testid=awcd-off]")?.textContent).toContain("not available");
    expect(el.querySelector("[data-testid=awcd-panel]")).toBeNull();
  });
});
