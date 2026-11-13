import {describe, expect, it} from "vitest";
import {gpsLabel, gpsOffset, gpsTone, markerSegments, parseGps, parseNavViz} from "../src/lib/navViz";

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

  it("parses a GPS fix and describes its quality", () => {
    const g = parseGps({lat: 42.36, lon: -71.09, alt: 10, status: 2, status_text: "GBAS fix (RTK)", std: [0.05, 0.08, 0.2], cov_type: "diagonal known", map: [3, 4], map_via: "anchor", t: 100});
    expect(g).not.toBeNull();
    expect(gpsTone(g!)).toBe("ok");
    expect(gpsLabel(g!, 100.5)).toBe("GBAS fix (RTK) · ±0.08 m");
    expect(gpsLabel(g!, 110)).toBe("GBAS fix (RTK) · ±0.08 m · 10 s old");
    expect(gpsOffset(g, {x: 0, y: 0})).toBeCloseTo(5);
    const noFix = parseGps({lat: 1, lon: 2, status: -1, std: null, map: null});
    expect(gpsTone(noFix!)).toBe("err");
    expect(gpsOffset(noFix, {x: 0, y: 0})).toBeNull();
    expect(parseGps({lat: "a", lon: 2})).toBeNull();
    expect(gpsTone(parseGps({lat: 1, lon: 2, status: 0, std: [12, 3, 4]})!)).toBe("err");
  });
});
