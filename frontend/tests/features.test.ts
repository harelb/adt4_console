import {describe, expect, it} from "vitest";
import {featureOn, takeLeaseOn} from "../src/lib/features";
import type {Health} from "../src/types";

const base: Health = {ok: true, version: "0", ros: true, fleetlink: false, neo4j: {}};

describe("optional features", () => {
  it("treats a feature the server does not report as on", () => {
    expect(featureOn(null, "fleet_core")).toBe(true);
    expect(featureOn(base, "fleet_core")).toBe(true);
    expect(featureOn({...base, features: {map_edit: true}}, "fleet_core")).toBe(true);
  });
  it("hides a feature the server reports off", () => {
    expect(featureOn({...base, features: {fleet_core: false}}, "fleet_core")).toBe(false);
  });
  it("offers Take lease unless the executor has no take_lease service", () => {
    expect(takeLeaseOn(base, "robot1")).toBe(true);
    expect(takeLeaseOn({...base, take_lease: {robot1: false}}, "robot1")).toBe(false);
    expect(takeLeaseOn({...base, take_lease: {robot1: true}}, "robot1")).toBe(true);
  });
});
