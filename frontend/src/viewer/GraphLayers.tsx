import {useEffect, useLayoutEffect, useMemo, useRef} from "react";
import * as THREE from "three";
import {useFrame, useThree} from "@react-three/fiber";
import {Html, Line} from "@react-three/drei";
import {LineSegments2} from "three/examples/jsm/lines/LineSegments2.js";
import {LineSegmentsGeometry} from "three/examples/jsm/lines/LineSegmentsGeometry.js";
import {LineMaterial} from "three/examples/jsm/lines/LineMaterial.js";
import type {GraphLayout, LayerBuffers, TextLabel, VizStyle} from "./layout";
import {defaultEdgeOn, EDGE_COLORS, HYDRA_EDGES, PLACE_BOUNDARY} from "./layout";
import {REGION_FILL_ALPHA, uniformOf, type ColorOpts} from "./style";
import {declutter, labelPrio, labelSize, MAX_LABELS, MAX_PLACE_LABELS, type LabelBox} from "../lib/labels";
import {pathColors, useViewer, type TrajPath, type TrajRobot} from "../stores/viewer";
import {useRobotColorOf} from "../lib/palette";
import {useMapEdit} from "../stores/mapEdit";
import {useViewStyle} from "../stores/viewStyle";

/** Displayed node positions per source, used by the click picker. */
export const pickRegistry = new Map<string, Map<string, LayerBuffers>>();

const nextPow2 = (n: number) => 2 ** Math.ceil(Math.log2(Math.max(n, 16)));

const sphere = new THREE.IcosahedronGeometry(1, 1);
const box = new THREE.BoxGeometry(1.4, 1.4, 1.4);

function NodeLayer({layer}: {layer: LayerBuffers}) {
  const capacity = nextPow2(layer.syms.length);
  const ref = useRef<THREE.InstancedMesh>(null);
  const invalidate = useThree(s => s.invalidate);
  const geom = layer.shape === "box" ? box : sphere;
  const material = useMemo(
    () => new THREE.MeshLambertMaterial({color: "#ffffff", transparent: layer.opacity < 1, opacity: layer.opacity}),
    [layer.opacity],
  );
  useLayoutEffect(() => () => material.dispose(), [material]);
  useLayoutEffect(() => {
    const m = ref.current;
    if (!m) return;
    const mat = new THREE.Matrix4();
    const col = new THREE.Color();
    const s = layer.size;
    for (let i = 0; i < layer.syms.length; i++) {
      mat.makeScale(s, s, s);
      mat.setPosition(layer.positions[i * 3], layer.positions[i * 3 + 1], layer.positions[i * 3 + 2]);
      m.setMatrixAt(i, mat);
      col.setRGB(layer.colors[i * 3], layer.colors[i * 3 + 1], layer.colors[i * 3 + 2], THREE.SRGBColorSpace);
      m.setColorAt(i, col);
    }
    m.count = layer.syms.length;
    m.instanceMatrix.needsUpdate = true;
    if (m.instanceColor) m.instanceColor.needsUpdate = true;
    m.computeBoundingSphere();
    invalidate();
  }, [layer, invalidate]);
  return <instancedMesh key={`${capacity}/${layer.shape}/${layer.opacity}`} ref={ref} args={[geom, material, capacity]} frustumCulled={false} />;
}

/** hydra RegionGrowingBoundaryPlugin boundary: one merged LINE_LIST with
 *  per-vertex state colours, 0.07 m wide in world units like the RViz marker. */
function PlaceBoundary({positions, colors}: {positions: Float32Array; colors: Float32Array}) {
  const obj = useMemo(() => {
    const g = new LineSegmentsGeometry();
    g.setPositions(positions);
    g.setColors(colors);
    const m = new LineMaterial({
      vertexColors: true,
      worldUnits: true,
      linewidth: PLACE_BOUNDARY.lineWidth,
      transparent: PLACE_BOUNDARY.lineAlpha < 1,
      opacity: PLACE_BOUNDARY.lineAlpha,
    });
    const l = new LineSegments2(g, m);
    l.frustumCulled = false;
    l.raycast = () => {};
    return l;
  }, [positions, colors]);
  useLayoutEffect(
    () => () => {
      obj.geometry.dispose();
      obj.material.dispose();
    },
    [obj],
  );
  return <primitive object={obj} />;
}

/** Translucent place-region fill (hydra RegionGrowingBoundaryPlugin, fill_alpha 0.3). */
function PlaceFill({positions, colors}: {positions: Float32Array; colors: Float32Array}) {
  const geom = useMemo(() => {
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    g.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    return g;
  }, [positions, colors]);
  useLayoutEffect(() => () => geom.dispose(), [geom]);
  return (
    <mesh geometry={geom} frustumCulled={false} renderOrder={-1}>
      <meshBasicMaterial vertexColors transparent opacity={PLACE_BOUNDARY.fillAlpha} side={THREE.DoubleSide} depthWrite={false} />
    </mesh>
  );
}

/** Room / region areas (the union of their places' footprints): translucent fill + outline in the region colour. */
function RegionHulls({fill, outline}: {fill: {positions: Float32Array; colors: Float32Array}; outline: {positions: Float32Array; colors: Float32Array} | null}) {
  const geom = useMemo(() => {
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(fill.positions, 3));
    g.setAttribute("color", new THREE.BufferAttribute(fill.colors, 3));
    return g;
  }, [fill]);
  useLayoutEffect(() => () => geom.dispose(), [geom]);
  return (
    <group name="region-hulls">
      <mesh geometry={geom} frustumCulled={false} renderOrder={-2} raycast={() => {}}>
        <meshBasicMaterial vertexColors transparent opacity={REGION_FILL_ALPHA} side={THREE.DoubleSide} depthWrite={false} />
      </mesh>
      {outline && <Segments positions={outline.positions} colors={outline.colors} opacity={0.95} />}
    </group>
  );
}

/** Node text labels, decluttered (lib/labels.ts): one DOM layer over the canvas,
 *  re-placed on every rendered frame; of the labels on screen the most important
 *  ones that do not overlap are shown (the selected node's always, then areas,
 *  objects, places, nearest the pointer first). Only the shown labels have a DOM
 *  element, from a pool as big as the cap, so a map with every place labelled
 *  (thousands of texts) still has a few hundred elements; a label keeps its
 *  element while it stays on screen. */
function TextLabels({texts}: {texts: TextLabel[]}) {
  const {gl, camera, invalidate} = useThree();
  const anchor = useRef<THREE.Group>(null);
  const host = useMemo(() => {
    const d = document.createElement("div");
    d.className = "viz-text-layer";
    return d;
  }, []);
  useLayoutEffect(() => {
    gl.domElement.parentElement?.appendChild(host);
    return () => host.remove();
  }, [gl, host]);
  // text index -> its element while shown; free elements are hidden
  const pool = useMemo(() => ({shown: new Map<number, HTMLDivElement>(), free: [] as HTMLDivElement[]}), []);
  useLayoutEffect(() => {
    // new texts: the old indices mean nothing, every element goes back to the pool
    for (const e of pool.shown.values()) {
      e.style.display = "none";
      pool.free.push(e);
    }
    pool.shown.clear();
    invalidate();
  }, [texts, pool, invalidate]);
  useLayoutEffect(
    () => () => {
      host.replaceChildren();
      pool.shown.clear();
      pool.free.length = 0;
    },
    [host, pool],
  );
  const sizes = useMemo(() => texts.map(t => labelSize(t.text, t.kind)), [texts]);
  const allPlaces = useMemo(() => texts.some(t => t.allPlaces), [texts]);
  // picked nodes: the inspected one and the map-edit selection
  const selected = useViewer(s => s.selected?.sym ?? null);
  const editSel = useMapEdit(s => s.selection);
  useEffect(() => invalidate(), [selected, editSel, invalidate]);
  // the pointer: labels near it win; a move of 40 px re-sorts them
  const pointer = useRef<{x: number; y: number} | null>(null);
  useEffect(() => {
    const el = gl.domElement;
    let last: {x: number; y: number} | null = null;
    const move = (e: PointerEvent) => {
      const r = el.getBoundingClientRect();
      pointer.current = {x: e.clientX - r.left, y: e.clientY - r.top};
      if (!last || Math.hypot(pointer.current.x - last.x, pointer.current.y - last.y) > 40) {
        last = pointer.current;
        invalidate();
      }
    };
    const leave = () => (pointer.current = null);
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerleave", leave);
    return () => {
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerleave", leave);
    };
  }, [gl, invalidate]);
  const v = useMemo(() => new THREE.Vector3(), []);
  useFrame(() => {
    const g = anchor.current;
    if (!g) return;
    let shown = true;
    for (let o: THREE.Object3D | null = g; o; o = o.parent) if (!o.visible) shown = false;
    const w = gl.domElement.clientWidth;
    const h = gl.domElement.clientHeight;
    const focus = pointer.current ?? {x: w / 2, y: h / 2};
    const picked = new Set(editSel);
    if (selected) picked.add(selected);
    const boxes: LabelBox[] = [];
    const idx: number[] = [];
    if (shown)
      texts.forEach((t, i) => {
        v.set(t.pos[0], t.pos[1], t.pos[2]).applyMatrix4(g.matrixWorld);
        // every place labelled: the nearer to the camera, the sooner
        const depth = t.allPlaces ? v.distanceTo(camera.position) : 0;
        v.project(camera);
        if (v.z > 1 || v.z < -1) return;
        const x = ((v.x + 1) / 2) * w;
        const y = ((1 - v.y) / 2) * h;
        const sz = sizes[i];
        if (x + sz.w / 2 < 0 || x - sz.w / 2 > w || y + sz.h / 2 < 0 || y - sz.h / 2 > h) return;
        boxes.push({x, y, w: sz.w, h: sz.h, prio: labelPrio(t.kind, Math.hypot(x - focus.x, y - focus.y), !!t.sym && picked.has(t.sym), depth), group: t.allPlaces ? "places" : "other"});
        idx.push(i);
      });
    // every place labelled: places have a budget of their own next to the others'
    const kept = allPlaces ? declutter(boxes, MAX_LABELS + MAX_PLACE_LABELS, 2, {places: MAX_PLACE_LABELS, other: MAX_LABELS}) : declutter(boxes, MAX_LABELS);
    const keep = new Map(kept.map(k => [idx[k], boxes[k]]));
    for (const [i, e] of pool.shown)
      if (!keep.has(i)) {
        e.style.display = "none";
        pool.shown.delete(i);
        pool.free.push(e);
      }
    for (const [i, b] of keep) {
      let e = pool.shown.get(i);
      if (!e) {
        e = pool.free.pop();
        if (!e) {
          e = document.createElement("div");
          host.appendChild(e);
        }
        const t = texts[i];
        e.className = `viz-text viz-text-${t.kind}`;
        e.textContent = t.text;
        e.style.display = "";
        pool.shown.set(i, e);
      }
      e.style.transform = `translate(${b.x.toFixed(1)}px, ${b.y.toFixed(1)}px) translate(-50%, -50%)`;
    }
  });
  return <group ref={anchor} />;
}

function Segments({positions, colors, color, opacity = 1}: {positions: Float32Array; colors?: Float32Array; color?: string; opacity?: number}) {
  const geom = useMemo(() => {
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    if (colors) g.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    return g;
  }, [positions, colors]);
  useLayoutEffect(() => () => geom.dispose(), [geom]);
  return (
    <lineSegments geometry={geom} frustumCulled={false}>
      <lineBasicMaterial vertexColors={Boolean(colors)} color={colors ? "#ffffff" : color} transparent={opacity < 1} opacity={opacity} />
    </lineSegments>
  );
}

/** Each robot's session, robot and source bag, at the start of its first path. */
function PathLabels({paths, robots, colors, fallback}: {paths: TrajPath[]; robots: TrajRobot[]; colors: Record<string, string> | null; fallback: string}) {
  const starts = useMemo(() => {
    const seen = new Map<string, [number, number, number]>();
    for (const p of paths) if (!seen.has(p.robot)) seen.set(p.robot, p.points[0]);
    return seen;
  }, [paths]);
  return (
    <group name="path-labels">
      {robots.map(r => {
        const at = starts.get(r.key);
        if (!at) return null;
        return (
          <Html key={r.key} position={at} zIndexRange={[15, 0]} style={{pointerEvents: "none"}}>
            <div className="path-label" style={{borderColor: colors?.[r.key] ?? fallback}} title={r.bag}>
              <b>{r.session ?? r.key}</b>
              {r.robot && r.robot !== r.session ? ` · ${r.robot}` : ""}
              {r.bag && <div className="path-label-bag">{bagName(r.bag)}</div>}
            </div>
          </Html>
        );
      })}
    </group>
  );
}

/** The bag's last path parts (``…/hard_robot_2_hamilton/recorded_data``): the full path is in the title. */
export function bagName(bag: string): string {
  const parts = bag.split("/").filter(Boolean);
  return parts.length > 2 ? `…/${parts.slice(-2).join("/")}` : bag;
}

export function GraphLayers({src, layout, showBBoxes, showTrajectory, trajectory, trajectoryRobots = [], edgeKinds, style = "classic", showText = false, placeLabels = false, colors, edgeInk = "#000000"}: {
  src: string;
  layout: GraphLayout;
  showBBoxes: boolean;
  showTrajectory: boolean;
  trajectory: TrajPath[] | null;
  trajectoryRobots?: TrajRobot[];
  edgeKinds: Record<string, boolean>;
  style?: VizStyle;
  showText?: boolean;
  /** every place labelled (Display › Place labels) */
  placeLabels?: boolean;
  colors?: ColorOpts;
  /** colour for hydra's black edges, swapped for a light grey on dark backgrounds */
  edgeInk?: string;
}) {
  const edgeUniform = uniformOf("edges", colors);
  const trajUniform = uniformOf("trajectory", colors);
  const hydraEdgeColor = (kind: string) => {
    const c = HYDRA_EDGES[kind]?.color ?? "#000000";
    return c === "#000000" ? edgeInk : c;
  };
  const hydra = style === "hydra";
  useLayoutEffect(() => {
    pickRegistry.set(src, new Map(layout.layers.map(l => [l.key, l])));
    return () => void pickRegistry.delete(src);
  }, [src, layout]);
  const trajPoints = useMemo(() => {
    if (trajectory && trajectory.length) return trajectory;
    return layout.agentPaths.length ? layout.agentPaths : null;
  }, [trajectory, layout.agentPaths]);
  const pathColor = useViewer(s => s.pathColor);
  const pathLabels = useViewer(s => s.pathLabels);
  const trajOpacity = useViewStyle(s => s.trajectoryOpacity);
  const robotColorOf = useRobotColorOf();
  const robots = useMemo(() => {
    if (trajectoryRobots.length) return trajectoryRobots;
    return [...new Set((trajPoints ?? []).map(p => p.robot))].map(key => ({key}));
  }, [trajectoryRobots, trajPoints]);
  const perRobot = useMemo(() => pathColors(robots, pathColor, robotColorOf), [robots, pathColor, robotColorOf]);
  const trajDefault = trajUniform ?? (hydra ? HYDRA_EDGES["agents-agents"].color : EDGE_COLORS["agents-agents"]);
  return (
    <group name={`graph:${src}`}>
      {layout.layers.map(l => (
        <NodeLayer key={l.key} layer={l} />
      ))}
      {showBBoxes && layout.bboxes.positions.length > 0 && <Segments positions={layout.bboxes.positions} colors={layout.bboxes.colors} opacity={0.9} />}
      {hydra && layout.placeFill && <PlaceFill positions={layout.placeFill.positions} colors={layout.placeFill.colors} />}
      {hydra && layout.placeBoundary && <PlaceBoundary positions={layout.placeBoundary.positions} colors={layout.placeBoundary.colors} />}
      {layout.regionFill && <RegionHulls fill={layout.regionFill} outline={layout.regionOutline} />}
      {Object.entries(layout.edges).map(([kind, pos]) =>
        (edgeKinds[kind] ?? defaultEdgeOn(kind, style)) && kind !== "agents-agents" ? (
          hydra ? (
            <Segments
              key={kind}
              positions={pos}
              colors={edgeUniform ? undefined : layout.edgeColors[kind]}
              color={edgeUniform ?? hydraEdgeColor(kind)}
              opacity={HYDRA_EDGES[kind]?.alpha ?? 0.5}
            />
          ) : (
            <Segments key={kind} positions={pos} color={edgeUniform ?? EDGE_COLORS[kind] ?? "#7f8b96"} opacity={kind === "places-places" ? 0.55 : 0.35} />
          )
        ) : null,
      )}
      {showTrajectory &&
        trajPoints?.map((p, i) => (
          <Line key={i} points={p.points} color={perRobot?.[p.robot] ?? trajDefault} lineWidth={hydra ? 2 : 2.5} transparent={trajOpacity < 1} opacity={trajOpacity} depthWrite={trajOpacity >= 1} />
        ))}
      {showTrajectory && pathLabels && trajPoints && <PathLabels paths={trajPoints} robots={robots} colors={perRobot} fallback={trajDefault} />}
      {(showText || placeLabels) && layout.texts.length > 0 && <TextLabels texts={layout.texts} />}
    </group>
  );
}
