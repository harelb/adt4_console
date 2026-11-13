// The AWCD base station's merged changes on the map, as RViz draws
// awcd_base_station/reconciled_objects: a wireframe box per change (green =
// added, red = removed) with a label. The change picked in the list is drawn
// wider. Global map frame: drawn as is (no per-robot alignment).
import {useEffect, useMemo} from "react";
import {Html, Line} from "@react-three/drei";
import {useThree} from "@react-three/fiber";
import {boxEdges, changeColor, changeLabel, labelAt, type AwcdChange} from "../lib/awcd";
import {useAwcd, useAwcdFeed} from "../stores/awcd";

const ORDER = 25;

function ChangeBox({c, focused, label}: {c: AwcdChange; focused: boolean; label: boolean}) {
  const pts = useMemo(() => boxEdges(c.center, c.dims, c.quat), [c.center, c.dims, c.quat]);
  const color = changeColor(c.kind);
  return (
    <group>
      <Line points={pts} segments color={color} lineWidth={focused ? 5 : 2.5} depthTest={false} transparent opacity={focused ? 1 : 0.9} renderOrder={ORDER} />
      {(label || focused) && (
        <Html position={labelAt(c)} center zIndexRange={[16, 0]} style={{pointerEvents: "none"}}>
          <div className={`awcd-label awcd-${c.kind}${focused ? " focused" : ""}`}>{changeLabel(c)}</div>
        </Html>
      )}
    </group>
  );
}

export function AwcdOverlay() {
  useAwcdFeed();
  const st = useAwcd(s => s.state);
  const show = useAwcd(s => s.show);
  const labels = useAwcd(s => s.labels);
  const focus = useAwcd(s => s.focus);
  const invalidate = useThree(s => s.invalidate);
  useEffect(() => invalidate(), [st, show, labels, focus, invalidate]); // the canvas renders on demand
  if (!show || !st?.changes.length) return null;
  return (
    <group name="awcd-changes">
      {st.changes.map(c => (
        <ChangeBox key={c.key} c={c} focused={focus === c.key} label={labels} />
      ))}
    </group>
  );
}
