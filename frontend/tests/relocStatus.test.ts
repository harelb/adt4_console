import {describe, expect, it} from "vitest";
import {healthNode, isBigJump, priorName, relocStatus} from "../src/lib/frameAlign";
import type {FrameAlign, Reloc} from "../src/types";

const FA: FrameAlign = {parent: "map", child: "hamilton/odom", T: [10, 5, 0, 1, 0, 0, 0], stamp: 100, ok: true, reason: "", age_s: 0.5};
const NOT: FrameAlign = {parent: "map", child: "hamilton/odom", T: null, stamp: null, ok: false, reason: "lookup failed"};

const reloc = (over: Partial<Reloc> = {}): Reloc => ({
  prior_lcs: 4,
  best_assoc: 12,
  last_lc_age_s: 18,
  lcs: [],
  jumps: 1,
  last_jump: {dxy: 35, dyaw: 178, age_s: 60},
  recent_jumps: [{dxy: 35, dyaw: 178, age_s: 60}],
  total: {dxy: 35, dyaw: 178},
  ...over,
});

describe("relocalization status", () => {
  it("is green with prior matches inside the prior map (one big jump = the relocalization)", () => {
    const s = relocStatus(FA, reloc(), 1.2, 2);
    expect(s.tone).toBe("ok");
    expect(s.text).toBe("relocalized · 4 matches · last 20 s ago");
    expect(s.title).toMatch(/4 loop closures against the prior map \(best 12 objects/);
    expect(s.title).toMatch(/nearest prior place 1\.2 m away/);
  });
  const none = () => reloc({prior_lcs: 0, last_lc_age_s: null, jumps: 0, last_jump: null, recent_jumps: [], total: {dxy: 0, dyaw: 0}});
  it("is amber before any prior match: the transform is only the initial estimate", () => {
    const s = relocStatus(FA, none(), 0.5);
    expect(s.tone).toBe("warn");
    expect(s.text).toBe("not relocalized · no prior-map match yet");
    // the old "starting guess" was read as "starting gauss" (2026-11-11 robot test)
    expect(`${s.text}\n${s.title}`).not.toMatch(/guess|gauss/i);
  });
  it("says 'matching' while the robot's ROMAN loop-closure node reports in", () => {
    const roman = {nickname: "hamilton/roman_lc", status: "nominal", notes: "No loop closures found for robot 0, submap 54."};
    const s = relocStatus(FA, none(), 0.5, 0, {roman, prior: "Nov11_Building45_v2 (recorded by euclid)"});
    expect(s.tone).toBe("warn");
    expect(s.text).toBe("not relocalized · matching prior map…");
    expect(s.title).toMatch(/no loop closure against prior map Nov11_Building45_v2 \(recorded by euclid\)/);
    expect(s.title).toMatch(/roman_lc\): nominal — No loop closures found for robot 0, submap 54/);
    // ROMAN without a heartbeat (or missing) is not "matching"
    expect(relocStatus(FA, none(), 0.5, 0, {roman: {...roman, status: "no_hb"}}).text).toBe("not relocalized · no prior-map match yet");
    expect(relocStatus(FA, none(), 0.5, 0, {roman: null}).title).toMatch(/roman_lc\): not in the robot's status table/);
  });
  it("flags a hydra_multi in error in the tooltip", () => {
    const s = relocStatus(FA, none(), 0.5, 0, {hydraMulti: {status: "error", notes: "time since last spin: 1.45e+03 [s]"}});
    expect(s.title).toMatch(/hydra_multi: error — time since last spin/);
    expect(relocStatus(FA, none(), 0.5, 0, {hydraMulti: {status: "nominal"}}).title).not.toMatch(/hydra_multi:/);
  });
  it("names the matched prior map and ROMAN prior ids when relocalized", () => {
    const lcs = [{prior: 1, live_submap: 3, prior_submap: 40, assoc: 9, age_s: 18}];
    const s = relocStatus(FA, reloc({lcs}), 1.2, 2, {prior: "Nov11_Building45_v2 (recorded by euclid)"});
    expect(s.text).toBe("relocalized · 4 matches · last 20 s ago");
    expect(s.title).toMatch(/4 loop closures against prior map Nov11_Building45_v2 \(recorded by euclid\) \(best 12 objects, last 20 s ago, ROMAN prior id 1\)/);
  });
  it("reads monitor rows and prior source labels", () => {
    const health = {nodes: [{nickname: "hamilton/roman_lc", status: "no_hb"}, {nickname: "hamilton/hydra_multi", status: "error"}]};
    expect(healthNode(health, "hamilton", "roman_lc")?.status).toBe("no_hb");
    expect(healthNode(health, "euclid", "roman_lc")).toBeNull();
    expect(healthNode({}, "euclid", "roman_lc")).toBeUndefined();
    expect(priorName("prior: Nov11_Building45_v2 (euclid)")).toBe("Nov11_Building45_v2 (recorded by euclid)");
    expect(priorName(null)).toBeNull();
  });
  it("is red when placed outside the prior map", () => {
    const s = relocStatus(FA, reloc(), 13.4);
    expect(s.tone).toBe("err");
    expect(s.text).toBe("suspect — 13 m outside the prior map");
  });
  it("is red when the placement jumped a second time", () => {
    const jumps = [{dxy: 0.2, dyaw: 0.5, age_s: 5}, {dxy: 9, dyaw: 4, age_s: 30}, {dxy: 35, dyaw: 178, age_s: 60}];
    const s = relocStatus(FA, reloc({jumps: 3, recent_jumps: jumps}), 2);
    expect(s.tone).toBe("err");
    expect(s.text).toBe("suspect — placement jumped 2×");
  });
  it("does not judge the inside/outside check without a shown prior", () => {
    const s = relocStatus(FA, reloc(), null);
    expect(s.tone).toBe("ok");
    expect(s.title).toMatch(/prior map not shown/);
  });
  it("falls back to the transform-only status for older agents and failed lookups", () => {
    expect(relocStatus(FA, undefined, 1).text).toBe("aligned to map (relocalized) · 0.5 s");
    expect(relocStatus(NOT, reloc(), 1).text).toBe("hamilton/odom frame — not relocalized");
    expect(relocStatus({...FA, child: "map"}, reloc(), 1).text).toBe("in map frame");
  });
  it("calls a jump big over 3 m or 30 degrees", () => {
    expect(isBigJump({dxy: 3.1, dyaw: 0})).toBe(true);
    expect(isBigJump({dxy: 0, dyaw: -31})).toBe(true);
    expect(isBigJump({dxy: 2.9, dyaw: 29})).toBe(false);
  });
});
