// The mission planner's chat shows the mission's agents ("Planning with"), not the
// live fleet's planner state.
import {afterEach, describe, expect, it} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {PlanningAgents, startText} from "../src/viewer/PlanningAgents";
import {useMission} from "../src/stores/mission";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLDivElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = host = null;
  useMission.setState({roster: []} as never);
});
function render() {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(<PlanningAgents />));
  return host;
}

describe("Planning with", () => {
  it("lists the mission's agents and their starts", () => {
    useMission.setState({roster: [{id: "spot1", kind: "spot", start: [-63.07, 45.89]}, {id: "husky1", kind: "husky", start: null}]} as never);
    const h = render();
    expect(h.querySelector("summary")!.textContent).toBe("Planning with: ● spot1 · ● husky1");
    const rows = [...h.querySelectorAll('[data-testid="planning-agent"]')].map(r => r.textContent);
    expect(rows[0]).toContain("spot1 spot · start (-63.1, 45.9)");
    expect(rows[1]).toContain("husky1 husky · start no start");
    expect(h.textContent).not.toMatch(/hamilton|euclid|Planner believes/);
  });

  it("says to add agents when there are none", () => {
    const h = render();
    expect(h.querySelector("summary")!.textContent).toContain("no agents yet: add them in the Mission tab");
  });

  it("startText", () => {
    expect(startText({id: "a", kind: "spot", start: [120.4, -3.25]} as never)).toBe("(120, -3.3)");
  });
});
