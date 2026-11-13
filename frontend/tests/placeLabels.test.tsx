// Display › "Place labels": every place labelled on Neo4j maps (Killian: ~2800
// places), decluttered with a budget of their own.
import {afterEach, describe, expect, it} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import type {NodeRecord} from "../src/codec";
import {GraphModel} from "../src/lib/graphModel";
import {declutter, labelPrio, MAX_LABELS, MAX_PLACE_LABELS, placeLabelText, type LabelBox} from "../src/lib/labels";
import {layoutGraph, TEXT_LIMITS} from "../src/viewer/layout";
import {DEFAULT_VIEW_STYLE, parseStoredStyle, placeLabelsKey, useViewStyle} from "../src/stores/viewStyle";
import {useViewer} from "../src/stores/viewer";
import {PlaceLabelsToggles} from "../src/viewer/panels";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const rec = (sym: string, layer: number, part: number, pos: [number, number, number], name = ""): NodeRecord => ({
  sym, layer, part, pos, label: -1, bbox: null, color: [0, 0, 0], name, active: false,
});
const base = {hiddenLayers: {}, edgeKinds: {}, flat: false, stackHeight: 2, style: "hydra" as const};

/** a Killian-sized map: `n` traversability places on a grid, a few objects */
function bigMap(n: number): GraphModel {
  const m = new GraphModel();
  for (let i = 0; i < n; i++) m.nodes.set(`t${i}`, rec(`t${i}`, 3, 2, [(i % 60) * 1.5, Math.floor(i / 60) * 1.5, 0], i === 7 ? "loading dock" : ""));
  for (let i = 0; i < 20; i++) m.nodes.set(`O${i}`, rec(`O${i}`, 2, 0, [i, 0, 0]));
  return m;
}

describe("place labels default per source kind", () => {
  it("is on for Neo4j maps and off (today's behaviour) for files and live maps", () => {
    expect(DEFAULT_VIEW_STYLE.placeLabels).toEqual({neo4j: true, other: false});
    expect(placeLabelsKey("neo4j")).toBe("neo4j");
    expect(placeLabelsKey("file")).toBe("other");
    expect(placeLabelsKey("live")).toBe("other");
    expect(placeLabelsKey(undefined)).toBe("other");
  });
  it("is remembered with the view style, per kind", () => {
    expect(parseStoredStyle(JSON.stringify({placeLabels: {neo4j: false}})).placeLabels).toEqual({neo4j: false, other: false});
    expect(parseStoredStyle(JSON.stringify({placeLabels: {other: true, neo4j: "x"}})).placeLabels).toEqual({neo4j: true, other: true});
    expect(parseStoredStyle(null).placeLabels).toEqual({neo4j: true, other: false});
  });
});

describe("every place labelled", () => {
  it("labels all 3000 places past the places cap, symbol as the UI prints it plus the name", () => {
    const m = bigMap(3000);
    expect(3000).toBeGreaterThan(TEXT_LIMITS.places!);
    const on = layoutGraph(m, {...base, text: false, placeLabels: true});
    const places = on.texts.filter(t => t.kind === "places");
    expect(places).toHaveLength(3000);
    expect(places.every(t => t.allPlaces)).toBe(true);
    expect(places.find(t => t.sym === "t7")!.text).toBe("t(7) loading dock");
    expect(places.find(t => t.sym === "t8")!.text).toBe("t(8)");
    expect(on.texts.some(t => t.kind === "objects")).toBe(false); // "Text labels" off: places only
    // off: today's behaviour, a big places layer gets no labels
    const off = layoutGraph(m, {...base, text: true, placeLabels: false});
    expect(off.texts.filter(t => t.kind === "places")).toHaveLength(0);
    expect(off.texts.filter(t => t.kind === "objects")).toHaveLength(20);
  });
  it("prints p and mesh places the same way", () => {
    expect(placeLabelText("p12")).toBe("p(12)");
    expect(placeLabelText("t(1242)", "door")).toBe("t(1242) door");
  });
});

describe("declutter with every place labelled", () => {
  const brute = (boxes: LabelBox[], max: number, gap = 2) => {
    const order = boxes.map((_, i) => i).sort((a, b) => boxes[b].prio - boxes[a].prio);
    const kept: number[] = [];
    for (const i of order) {
      if (kept.length >= max) break;
      const a = boxes[i];
      if (kept.every(j => Math.abs(a.x - boxes[j].x) * 2 >= a.w + boxes[j].w + gap * 2 || Math.abs(a.y - boxes[j].y) * 2 >= a.h + boxes[j].h + gap * 2)) kept.push(i);
    }
    return kept;
  };
  // deterministic pseudo-random boxes
  let seed = 1;
  const rnd = () => ((seed = (seed * 16807) % 2147483647) - 1) / 2147483646;
  const boxes = (n: number): LabelBox[] => Array.from({length: n}, () => ({x: rnd() * 1600 - 50, y: rnd() * 900 - 20, w: 20 + rnd() * 200, h: 10 + rnd() * 14, prio: rnd()}));

  it("keeps the same labels as the all-pairs test", () => {
    for (const n of [10, 300, 3000]) {
      const b = boxes(n);
      expect(declutter(b, 120)).toEqual(brute(b, 120));
      expect(declutter(b, 100_000)).toEqual(brute(b, 100_000));
    }
  });
  it("never shows two overlapping labels and caps the places", () => {
    const b = boxes(3000).map((x, i) => ({...x, group: i % 10 ? "places" : "other"}));
    const kept = declutter(b, MAX_LABELS + MAX_PLACE_LABELS, 2, {places: MAX_PLACE_LABELS, other: MAX_LABELS});
    expect(kept.filter(i => b[i].group === "places").length).toBeLessThanOrEqual(MAX_PLACE_LABELS);
    expect(kept.filter(i => b[i].group === "other").length).toBeLessThanOrEqual(MAX_LABELS);
    for (const i of kept)
      for (const j of kept) if (i !== j) expect(Math.abs(b[i].x - b[j].x) * 2 >= b[i].w + b[j].w + 4 || Math.abs(b[i].y - b[j].y) * 2 >= b[i].h + b[j].h + 4).toBe(true);
  });
  it("a place budget of its own: objects do not crowd places out", () => {
    // 200 objects and 200 places, none overlapping: objects win on priority
    const grid = (k: number, kind: string, group: string): LabelBox => ({x: (k % 40) * 40, y: Math.floor(k / 40) * 20, w: 30, h: 12, prio: labelPrio(kind, 0, false), group});
    const b = [...Array.from({length: 200}, (_, k) => grid(k, "objects", "other")), ...Array.from({length: 200}, (_, k) => grid(k + 200, "places", "places"))];
    expect(declutter(b, MAX_LABELS).every(i => b[i].group === "other")).toBe(true); // shared budget: no places
    const kept = declutter(b, MAX_LABELS + MAX_PLACE_LABELS, 2, {places: MAX_PLACE_LABELS, other: MAX_LABELS});
    expect(kept.filter(i => b[i].group === "places")).toHaveLength(MAX_PLACE_LABELS);
    expect(kept.filter(i => b[i].group === "other")).toHaveLength(MAX_LABELS);
  });
  it("shows nearer places first", () => {
    const near = labelPrio("places", 100, false, 5);
    const far = labelPrio("places", 100, false, 60);
    expect(near).toBeGreaterThan(far);
    const b: LabelBox[] = [
      {x: 100, y: 100, w: 60, h: 10, prio: far},
      {x: 105, y: 102, w: 60, h: 10, prio: near},
    ];
    expect(declutter(b, 10)).toEqual([1]);
  });
  it("takes a few ms for 3000 labels", () => {
    const b = boxes(3000).map(x => ({...x, group: "places"}));
    const t0 = performance.now();
    for (let k = 0; k < 20; k++) declutter(b, MAX_LABELS + MAX_PLACE_LABELS, 2, {places: MAX_PLACE_LABELS, other: MAX_LABELS});
    expect((performance.now() - t0) / 20).toBeLessThan(15);
  });
});

describe("Place labels toggle", () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    useViewStyle.setState({placeLabels: {neo4j: true, other: false}});
    useViewer.setState({sources: {}});
  });
  const src = (id: string, kind: "neo4j" | "file" | "live") => ({id, kind, label: id}) as unknown as ReturnType<typeof useViewer.getState>["sources"][string];

  it("shows one toggle per kind of source loaded, on for Neo4j", () => {
    useViewer.setState({sources: {"neo4j:prior": src("neo4j:prior", "neo4j"), "file:a": src("file:a", "file")}});
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root!.render(<PlaceLabelsToggles />));
    const boxes = [...host.querySelectorAll<HTMLLabelElement>("label.toggle")];
    expect(boxes.map(b => b.textContent)).toEqual(["Place labels (Neo4j maps)", "Place labels (files and live maps)"]);
    const inputs = boxes.map(b => b.querySelector("input")!);
    expect(inputs.map(i => i.checked)).toEqual([true, false]);
    act(() => inputs[0].click());
    expect(useViewStyle.getState().placeLabels).toEqual({neo4j: false, other: false});
  });
  it("one source kind: just 'Place labels'", () => {
    useViewer.setState({sources: {"neo4j:live": src("neo4j:live", "neo4j")}});
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root!.render(<PlaceLabelsToggles />));
    expect(host.textContent).toBe("Place labels");
  });
});
