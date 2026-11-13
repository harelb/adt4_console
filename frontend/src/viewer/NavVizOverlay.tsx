// On-demand planner overlay in the 3D view (off by default): the Husky's MPPI
// plan / reference / sampled rollouts and its mid-range plan, drawn like
// phoenix-r2's gauss_mppi_debug.rviz but in the robot's colour, through the
// robot's frame alignment (AlignedGroup). Plus its Live-overlays panel row.
import {useMemo} from "react";
import * as THREE from "three";
import {Line} from "@react-three/drei";
import {useLiveRobots, useRobots} from "../stores/robots";
import {toggleNavViz, useNavViz, useNavVizLease} from "../stores/navViz";
import {NAV_LAYERS, markerSegments, type NavLayer} from "../lib/navViz";
import {shade, useRobotColorOf} from "../lib/palette";
import {AlignedGroup} from "./AlignedGroup";
import {useBackground} from "./background";
import {readableOn} from "./style";

const LIFT = 0.25; // above the occupancy grid and executor paths

function NavPoints({pts, color, opacity}: {pts: [number, number, number][]; color: string; opacity: number}) {
  const geometry = useMemo(() => {
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(new Float32Array(pts.flatMap(p => [p[0], p[1], p[2] + LIFT])), 3));
    return g;
  }, [pts]);
  return (
    <points geometry={geometry} renderOrder={3}>
      <pointsMaterial size={0.12} color={color} transparent opacity={opacity} depthWrite={false} />
    </points>
  );
}

function LayerLines({layer, color, width, opacity, dashed}: {layer: NavLayer; color: string; width: number; opacity: number; dashed?: boolean}) {
  // one LineSegments for all line markers of the layer (rollouts are many short strips)
  const segs = useMemo(() => {
    const out: [number, number, number][] = [];
    for (const m of layer.markers) if (m.kind !== "points") for (const p of markerSegments(m)) out.push([p[0], p[1], p[2] + LIFT]);
    return out;
  }, [layer]);
  const points = useMemo(() => layer.markers.filter(m => m.kind === "points").flatMap(m => m.pts), [layer]);
  return (
    <>
      {segs.length >= 2 && <Line points={segs} segments color={color} lineWidth={width} transparent opacity={opacity} dashed={dashed} dashSize={0.3} gapSize={0.2} depthWrite={false} />}
      {points.length > 0 && <NavPoints pts={points} color={color} opacity={opacity} />}
    </>
  );
}

function PlannerLayer({robot}: {robot: string}) {
  const p = useNavViz(s => s.planner[robot]);
  const on = useNavViz(s => !!s.plannerOn[robot]);
  useNavVizLease(robot, "planner", on);
  const base = useRobotColorOf()(robot);
  const bg = useBackground();
  if (!on || !p) return null;
  return (
    <>
      {NAV_LAYERS.map(spec => {
        const layer = p.layers[spec.key];
        if (!layer || !layer.markers.length) return null;
        const color = readableOn(shade(base, spec.shade), bg, 2);
        return (
          <AlignedGroup key={spec.key} robot={robot} frame={layer.frame}>
            <LayerLines layer={layer} color={color} width={spec.width} opacity={spec.opacity} dashed={spec.dashed} />
          </AlignedGroup>
        );
      })}
    </>
  );
}

/** 3D: every live robot's planner markers (each only while toggled on). */
export function NavVizOverlay() {
  const robots = useLiveRobots();
  return (
    <>
      {robots.map(r => (
        <PlannerLayer key={r} robot={r} />
      ))}
    </>
  );
}

/** Panel row for one robot: the local planner toggle and what arrived. */
export function NavVizRows({robot}: {robot: string}) {
  const on = useNavViz(s => !!s.plannerOn[robot]);
  const p = useNavViz(s => s.planner[robot]);
  const err = useNavViz(s => s.error[robot]);
  const topics = useNavViz(s => s.topics[robot]);
  const platform = useRobots(s => s.state[robot]?.platform);
  const spot = platform === "spot" || platform === undefined;
  return (
    <div className="nav-viz-rows">
      <label className="mini-check" title={spot ? "Spot's planner paths are the smooth / MLP paths below" : "MPPI plan, reference, rollouts and the mid-range plan (phoenix navigation stack)"}>
        <input type="checkbox" checked={on} disabled={spot} onChange={e => toggleNavViz(robot, e.target.checked)} data-testid={`navviz-planner-${robot}`} />
        local planner (MPPI){spot ? " · Husky only" : ""}
      </label>
      {on && (
        <div className="muted small mono">
          {err
            ? err
            : p
              ? Object.values(p.layers)
                  .map(l => `${l.label} ${l.markers.length}`)
                  .join(" · ")
              : topics && !topics.length
                ? "no planner markers for this platform"
                : "waiting for planner markers"}
        </div>
      )}
    </div>
  );
}

/** Panel footer: the planner legend. */
export function NavVizLegend() {
  return <div className="muted small">MPPI: plan (thick) · reference (light) · rollouts (faint) · mid-range (dashed)</div>;
}
