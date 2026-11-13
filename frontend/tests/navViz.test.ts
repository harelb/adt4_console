import {describe, expect, it} from "vitest";
import {markerSegments, parseNavViz} from "../src/lib/navViz";

describe("nav_viz payloads", () => {
  it("parses planner layers and drops malformed markers", () => {
    const p = parseNavViz({
      t: 5,
      layers: {
        srp_plan: {label: "MPPI plan", frame: "gauss/odom", t: 4, markers: [
          {ns: "plan", id: 0, kind: "strip", pts: [[0, 0, 0], [1, 0, 0], ["x", 0, 0]], color: [1, 0.5, 0, 1]},
          {ns: "bad", id: 1, kind: "mesh", pts: [[0, 0, 0]]},
          {ns: "empty", id: 2, kind: "points", pts: []},
        ]},
      },
    });
    expect(p?.layers.srp_plan.frame).toBe("gauss/odom");
    expect(p?.layers.srp_plan.markers).toHaveLength(1);
    expect(p?.layers.srp_plan.markers[0].pts).toEqual([[0, 0, 0], [1, 0, 0]]);
    expect(parseNavViz(null)).toBeNull();
    expect(parseNavViz({layers: 3})).toBeNull();
  });

  it("turns strips into segment pairs", () => {
    expect(markerSegments({ns: "", id: 0, kind: "strip", color: null, pts: [[0, 0, 0], [1, 0, 0], [2, 0, 0]]})).toEqual([
      [0, 0, 0], [1, 0, 0], [1, 0, 0], [2, 0, 0],
    ]);
  });
});
