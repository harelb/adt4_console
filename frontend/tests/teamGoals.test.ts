import {describe, expect, it} from "vitest";
import {goalRobots, goalUseRobots, planAutoSend, sendLabel} from "../src/lib/autoSend";
import {toChatItem} from "../src/viewer/ChatPanel";

const CHAIRS = "(and (visited-object o2) (visited-object o8) (visited-object o11) (visited-object o15))";
const BOTH = [{predicate: "use-robots", symbols: ["euclid", "hamilton"]}];
const online = [
  {name: "gauss", kind: "phoenix"},
  {name: "euclid", kind: "spot"},
  {name: "hamilton", kind: "spot"},
];

describe("team goals", () => {
  it("reads use-robots in both constraint shapes", () => {
    expect(goalUseRobots(BOTH)).toEqual(["euclid", "hamilton"]);
    expect(goalUseRobots([["use-robots", "Euclid"], ["forbidden-poi", "o1"]])).toEqual(["euclid"]);
    expect(goalUseRobots(undefined)).toEqual([]);
  });

  it("auto-sends a use-robots team goal once, through one of its robots", () => {
    const plan = planAutoSend(CHAIRS, online, [], BOTH);
    expect(plan.via).toBe("euclid");
    expect(plan.robots).toEqual(["euclid", "hamilton"]);
  });

  it("does not send a team goal whose robot is offline", () => {
    const plan = planAutoSend(CHAIRS, online.slice(0, 2), [], BOTH);
    expect(plan.via).toBeNull();
    expect(plan.blocked).toContain("hamilton");
  });

  it("sends a combined pick goal once, through its first robot", () => {
    const goal = "(and (holding hamilton o1) (holding euclid o20))";
    expect(goalRobots(goal)).toEqual(["hamilton", "euclid"]);
    expect(planAutoSend(goal, online, [])).toMatchObject({via: "hamilton", robots: ["hamilton", "euclid"]});
  });

  it("labels the send button with the team", () => {
    expect(sendLabel("euclid", ["euclid", "hamilton"])).toBe("team (euclid, hamilton)");
    expect(sendLabel("euclid", [])).toBe("team");
    expect(sendLabel("hamilton", null)).toBe("hamilton");
    expect(sendLabel(null, undefined)).toBe("robot");
  });

  it("keeps the team and replaces flags of a goal proposal", () => {
    expect(toChatItem({type: "goal", kind: "pddl", body: {goal: CHAIRS, constraints: BOTH}, robot: "euclid", team: ["euclid", "hamilton"], replaces: true})).toMatchObject({
      robot: "euclid",
      team: ["euclid", "hamilton"],
      replaces: true,
    });
    expect(toChatItem({type: "goal", kind: "pddl", body: {goal: "(holding euclid o1)"}, robot: "euclid"})).toMatchObject({team: null, replaces: false});
  });
});
