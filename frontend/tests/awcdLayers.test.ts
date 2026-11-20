import {describe, expect, it} from "vitest";
import {awcdRobots, changeVisible, type AwcdState} from "../src/lib/awcd";

describe("AWCD per-robot layers", () => {
  it("shows a change unless every robot it is attributed to is hidden", () => {
    expect(changeVisible({robots: ["a", "b"]}, {a: true})).toBe(true);
    expect(changeVisible({robots: ["a", "b"]}, {a: true, b: true})).toBe(false);
    expect(changeVisible({robots: []}, {})).toBe(true);
    expect(changeVisible({robots: []}, {"?": true})).toBe(false);
  });
  it("lists robots from attribution and raw reports", () => {
    const st = {robots: {euclid: {t: 1, added: 0, removed: 1}}, changes: [{robots: ["hamilton"]}]} as unknown as AwcdState;
    expect(awcdRobots(st)).toEqual(["euclid", "hamilton"]);
    expect(awcdRobots(null)).toEqual([]);
  });
});
