// The AWCD base station's merged changes on the map, as RViz draws
// awcd_base_station/reconciled_objects: a wireframe box per change (green =
// added, red = removed) with a label. The change picked in the list is drawn
// wider. Global map frame: drawn as is (no per-robot alignment). Layers ->
// Change detection hides one robot's changes, and can add each robot's raw
// report (/awcd_changes, dashed, drawn through the robot's frame alignment).
import {useEffect, useMemo} from "react";
import {Html, Line} from "@react-three/drei";
import {useThree} from "@react-three/fiber";
import {boxEdges, changeColor, changeLabel, changeVisible, labelAt, type AwcdChange} from "../lib/awcd";
import {useAwcd, useAwcdFeed} from "../stores/awcd";
import {AlignedGroup} from "./AlignedGroup";

const ORDER = 25;

function ChangeBox({c, focused, label, raw}: {c: AwcdChange; focused: boolean; label: boolean; raw?: boolean}) {
  const pts = useMemo(() => boxEdges(c.center, c.dims, c.quat), [c.center, c.dims, c.quat]);
  const color = changeColor(c.kind);
  return (
    <group>
      <Line
        points={pts}
        segments
        color={color}
        lineWidth={focused ? 5 : raw ? 1.5 : 2.5}
        dashed={raw}
        dashSize={0.15}
        gapSize={0.1}
        depthTest={false}
        transparent
        opacity={focused ? 1 : raw ? 0.7 : 0.9}
        renderOrder={ORDER}
      />
      {(label || focused) && (
        <Html position={labelAt(c)} center zIndexRange={[16, 0]} style={{pointerEvents: "none"}}>
          <div className={`awcd-label awcd-${c.kind}${focused ? " focused" : ""}${raw ? " raw" : ""}`}>{raw ? `raw ${changeLabel(c)}` : changeLabel(c)}</div>
        </Html>
      )}
    </group>
  );
}

export function AwcdOverlay() {
  useAwcdFeed();
  const st = useAwcd(s => s.state);
  const show = useAwcd(s => s.show);
  const showRaw = useAwcd(s => s.showRaw);
  const hidden = useAwcd(s => s.hiddenRobots);
  const labels = useAwcd(s => s.labels);
  const focus = useAwcd(s => s.focus);
  const invalidate = useThree(s => s.invalidate);
  useEffect(() => invalidate(), [st, show, showRaw, hidden, labels, focus, invalidate]); // the canvas renders on demand
  if (!st) return null;
  const rec = show ? st.changes.filter(c => changeVisible(c, hidden)) : [];
  const raw = showRaw ? Object.entries(st.raw ?? {}).filter(([r, rep]) => !hidden[r] && rep.changes.length) : [];
  if (!rec.length && !raw.length) return null;
  return (
    <group name="awcd-changes">
      {rec.map(c => (
        <ChangeBox key={c.key} c={c} focused={focus === c.key} label={labels} />
      ))}
      {raw.map(([robot, rep]) => (
        <AlignedGroup key={`raw-${robot}`} robot={robot} frame={rep.frame_id || null}>
          {rep.changes.map(c => (
            <ChangeBox key={c.key} c={c} focused={focus === c.key} label={labels} raw />
          ))}
        </AlignedGroup>
      ))}
    </group>
  );
}
