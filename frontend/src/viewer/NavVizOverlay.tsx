// On-demand navigation overlays in the 3D view (all off by default):
//  * planner: the Husky's MPPI plan / reference / sampled rollouts and its
//    mid-range plan, drawn like phoenix-r2's gauss_mppi_debug.rviz but in the
//    robot's colour, through the robot's frame alignment (AlignedGroup);
//  * GPS: the fix as a ring (radius = horizontal 1-sigma) at its map x/y, a
//    line to the relocalized pose, and a label with fix quality.
// Plus the Live-overlays panel rows: toggles and "jump to" pose / GPS buttons.
import {useMemo} from "react";
import * as THREE from "three";
import {Html, Line} from "@react-three/drei";
import {frameAlignOf, useLiveRobots, useRobots} from "../stores/robots";
import {toggleNavViz, useNavViz, useNavVizLease} from "../stores/navViz";
import {NAV_LAYERS, gpsLabel, gpsOffset, gpsTone, horizSigma, markerSegments, type GpsFix, type NavLayer} from "../lib/navViz";
import {poseInGlobal} from "../lib/frameAlign";
import {shade, useRobotColorOf} from "../lib/palette";
import {useViewer} from "../stores/viewer";
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

const TONE_HEX = {ok: "#2e9d5b", warn: "#d4a017", err: "#d64545"};

function useGlobalPose(robot: string) {
  const pose = useRobots(s => s.state[robot]?.pose);
  const fa = useRobots(s => frameAlignOf(robot, s));
  return useMemo(() => (pose ? poseInGlobal(pose, fa) : null), [pose, fa]);
}

function GpsMarker({robot, g}: {robot: string; g: GpsFix}) {
  const base = useRobotColorOf()(robot);
  const gp = useGlobalPose(robot);
  const showLine = useNavViz(s => s.gpsLine);
  if (!g.map) return null;
  const z = (gp?.z ?? 0) + 0.3;
  const sigma = horizSigma(g);
  const r = Math.min(Math.max(sigma ?? 0.5, 0.25), 25);
  const tone = TONE_HEX[gpsTone(g)];
  const off = gpsOffset(g, gp);
  return (
    <group>
      <group position={[g.map[0], g.map[1], z]}>
        <mesh renderOrder={4}>
          <ringGeometry args={[r * 0.9, r, 48]} />
          <meshBasicMaterial color={tone} transparent opacity={0.85} side={THREE.DoubleSide} depthWrite={false} />
        </mesh>
        <mesh renderOrder={4}>
          <circleGeometry args={[0.15, 20]} />
          <meshBasicMaterial color={base} depthWrite={false} />
        </mesh>
        <Html position={[0, 0, 0.6]} center zIndexRange={[16, 0]} style={{pointerEvents: "none"}}>
          <div className={`gps-label tone-${gpsTone(g)}`} data-testid={`gps-label-${robot}`}>
            {robot} GPS · {gpsLabel(g)}
            {off !== null ? ` · ${off.toFixed(1)} m from pose` : ""}
          </div>
        </Html>
      </group>
      {showLine && gp && <Line points={[[g.map[0], g.map[1], z], [gp.x, gp.y, z]]} color={base} lineWidth={1.5} dashed dashSize={0.25} gapSize={0.15} />}
    </group>
  );
}

function GpsLayer({robot}: {robot: string}) {
  const on = useNavViz(s => !!s.gpsOn[robot]);
  const g = useNavViz(s => s.gps[robot]);
  useNavVizLease(robot, "gps", on);
  return on && g ? <GpsMarker robot={robot} g={g} /> : null;
}

/** 3D: every live robot's planner markers and GPS fix (each only while toggled on). */
export function NavVizOverlay() {
  const robots = useLiveRobots();
  return (
    <>
      {robots.map(r => (
        <group key={r}>
          <PlannerLayer robot={r} />
          <GpsLayer robot={r} />
        </group>
      ))}
    </>
  );
}

/** Panel rows for one robot: planner / GPS toggles and jump-to buttons. */
export function NavVizRows({robot}: {robot: string}) {
  const st = useNavViz();
  const platform = useRobots(s => s.state[robot]?.platform);
  const gp = useGlobalPose(robot);
  const fly = useViewer(s => s.fly);
  const g = st.gps[robot];
  const err = st.error[robot] ?? {};
  const topics = st.topics[robot];
  const off = gpsOffset(g, gp);
  const spot = platform === "spot" || platform === undefined;
  return (
    <div className="nav-viz-rows">
      <label className="mini-check" title={spot ? "Spot's planner paths are the smooth / MLP paths above" : "MPPI plan, reference, rollouts and the mid-range plan (phoenix navigation stack)"}>
        <input type="checkbox" checked={!!st.plannerOn[robot]} disabled={spot} onChange={e => toggleNavViz(robot, "planner", e.target.checked)} data-testid={`navviz-planner-${robot}`} />
        local planner (MPPI){spot ? " · Husky only" : ""}
      </label>
      <label className="mini-check" title="The robot's GPS fix (NavSatFix) next to its relocalized pose">
        <input type="checkbox" checked={!!st.gpsOn[robot]} onChange={e => toggleNavViz(robot, "gps", e.target.checked)} data-testid={`navviz-gps-${robot}`} />
        GPS fix
      </label>
      <span className="row gap-sm">
        <button type="button" className="btn btn-xs" disabled={!gp} title="Centre the view on the robot's relocalized pose" onClick={() => gp && fly([gp.x, gp.y, gp.z])} data-testid={`jump-pose-${robot}`}>
          Jump to pose
        </button>
        <button type="button" className="btn btn-xs" disabled={!g?.map} title={g && !g.map ? "No GPS anchor for this map and no ADT4_ORIGIN_UTM: the fix has no map position" : "Centre the view on the GPS fix"} onClick={() => g?.map && fly([g.map[0], g.map[1], gp?.z ?? 0])} data-testid={`jump-gps-${robot}`}>
          Jump to GPS
        </button>
      </span>
      {st.plannerOn[robot] && (
        <div className="muted small mono">
          {err.planner
            ? err.planner
            : st.planner[robot]
              ? Object.entries(st.planner[robot].layers)
                  .map(([, l]) => `${l.label} ${l.markers.length}`)
                  .join(" · ")
              : topics && !topics.length
                ? "no planner markers for this platform"
                : "waiting for planner markers"}
        </div>
      )}
      {st.gpsOn[robot] && (
        <div className="muted small mono" title={g ? `${g.lat.toFixed(7)}, ${g.lon.toFixed(7)} · cov ${g.cov_type} · map via ${g.map_via ?? "none"}` : undefined}>
          {err.gps ? err.gps : g ? `${gpsLabel(g)}${off !== null ? ` · ${off.toFixed(1)} m from pose` : ""}${g.map ? "" : " · no map position (no GPS anchor)"}` : "waiting for a GPS fix"}
        </div>
      )}
    </div>
  );
}

/** Panel footer: the GPS-to-pose line toggle and the planner legend. */
export function NavVizLegend() {
  const gpsLine = useNavViz(s => s.gpsLine);
  const set = useNavViz(s => s.set);
  return (
    <div className="row gap-sm wrap">
      <label className="mini-check">
        <input type="checkbox" checked={gpsLine} onChange={e => set({gpsLine: e.target.checked})} /> line GPS fix → pose
      </label>
      <span className="muted small">MPPI: plan (thick) · reference (light) · rollouts (faint) · mid-range (dashed)</span>
    </div>
  );
}
