// Text-label decluttering for the 3D view: of all the node labels on screen,
// keep the most important ones that do not overlap (screen-space collision
// culling), at most `max`. Zoomed out, a dense map shows a readable handful;
// zooming in frees room and more appear. The selected / hovered node's label
// always wins (GraphLayers.tsx TextLabels).

import {uiSym} from "./mapEdit";

export interface LabelBox {
  /** centre, px */
  x: number;
  y: number;
  w: number;
  h: number;
  prio: number;
  /** budget group (`declutter`'s `caps`) */
  group?: string;
}

/** Screen grid cell for the overlap test, px (a few labels wide). */
const CELL = 64;

/** Indices of the labels to show: by priority, each clear of those already kept,
 *  at most `max`, and at most `caps[group]` of a group. The kept boxes are binned
 *  in a screen grid, so a candidate is tested only against its neighbours: a
 *  few thousand labels on screen (every place of a large map) stay cheap. */
export function declutter(boxes: readonly LabelBox[], max: number, gap = 2, caps?: Readonly<Record<string, number>>): number[] {
  const order = boxes.map((_, i) => i).sort((a, b) => boxes[b].prio - boxes[a].prio);
  const kept: number[] = [];
  const used = new Map<string, number>();
  const grid = new Map<number, number[]>();
  const cells = (b: LabelBox, f: (key: number) => boolean | void): boolean => {
    const x0 = Math.floor((b.x - b.w / 2 - gap) / CELL);
    const x1 = Math.floor((b.x + b.w / 2 + gap) / CELL);
    const y0 = Math.floor((b.y - b.h / 2 - gap) / CELL);
    const y1 = Math.floor((b.y + b.h / 2 + gap) / CELL);
    for (let cx = x0; cx <= x1; cx++) for (let cy = y0; cy <= y1; cy++) if (f((cx + 32768) * 65536 + (cy + 32768)) === false) return false;
    return true;
  };
  for (const i of order) {
    if (kept.length >= max) break;
    const a = boxes[i];
    const cap = a.group !== undefined ? caps?.[a.group] : undefined;
    if (cap !== undefined && (used.get(a.group!) ?? 0) >= cap) continue;
    const clear = cells(a, key =>
      (grid.get(key) ?? []).every(j => {
        const b = boxes[j];
        return Math.abs(a.x - b.x) * 2 >= a.w + b.w + gap * 2 || Math.abs(a.y - b.y) * 2 >= a.h + b.h + gap * 2;
      }),
    );
    if (!clear) continue;
    kept.push(i);
    if (a.group !== undefined) used.set(a.group, (used.get(a.group) ?? 0) + 1);
    cells(a, key => {
      const c = grid.get(key);
      if (c) c.push(i);
      else grid.set(key, [i]);
    });
  }
  return kept;
}

/** Size estimate of a label (the CSS of .viz-text-<kind>), px. */
export function labelSize(text: string, kind: string): {w: number; h: number} {
  // areas: bold upper-case with letter spacing on a padded pill (styles.css .viz-text-rooms)
  const [cw, h, pad] = kind === "places" ? [5.2, 12, 6] : kind === "rooms" ? [10, 22, 20] : [6.2, 14, 6];
  return {w: text.length * cw + pad, h};
}

const KIND_WEIGHT: Record<string, number> = {rooms: 4, buildings: 4, agents: 3, objects: 2, places: 1};

/** Screen px a metre of distance from the camera weighs like (labelPrio `depthM`). */
const DEPTH_PX_PER_M = 12;

/** Priority: a picked node first, then by kind (areas over objects over
 *  places), then nearest the pointer (or the view centre) and, given its
 *  distance from the camera, nearest the camera. */
export function labelPrio(kind: string, distPx: number, picked: boolean, depthM = 0): number {
  return (picked ? 1e7 : 0) + (KIND_WEIGHT[kind] ?? 1) * 1e5 - Math.min(distPx + depthM * DEPTH_PX_PER_M, 99_999);
}

/** Labels kept on screen at once. */
export const MAX_LABELS = 120;
/** With every place labelled (Display › Place labels): place labels kept on
 *  screen at once, a budget of their own so objects and areas do not crowd them out. */
export const MAX_PLACE_LABELS = 150;

/** Label of a place when every place is labelled: its symbol as the UI prints
 *  it (`uiSym`), then its name if it has one. */
export function placeLabelText(sym: string, name?: string | null): string {
  const s = uiSym(sym);
  return name ? `${s} ${name}` : s;
}
