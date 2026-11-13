// The chat's planner toggle (Fast Downward | GRSTAPS-X): remembered per browser,
// greyed out with the reason when the base does not run GRSTAPS-X, and sent with
// each chat turn as `engine` (the backend gives the model that engine's goal rules
// and puts it on the goal cards).
import {afterEach, describe, expect, it} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {CHAT_ENGINE_KEY, chatEngineNow, effectiveChatEngine, useChatEngine, type ChatEngineInfo} from "../src/stores/chatEngine";
import {ChatEnginePicker} from "../src/viewer/ChatEnginePicker";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLDivElement | null = null;
function render(el: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(el));
  return host;
}
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  localStorage.removeItem(CHAT_ENGINE_KEY);
  useChatEngine.setState({engine: "fast_downward", engines: []});
});

const FD: ChatEngineInfo = {id: "fast_downward", label: "Fast Downward", available: true, reason: null};
const GR_ON: ChatEngineInfo = {id: "grstaps", label: "GRSTAPS-X", available: true, reason: null};
const GR_OFF: ChatEngineInfo = {id: "grstaps", label: "GRSTAPS-X", available: false, reason: "no GRSTAPS-X planner is running"};

describe("chat planner toggle", () => {
  it("is the engine sent with a turn, and falls back to Fast Downward when GRSTAPS-X is off", () => {
    expect(effectiveChatEngine("grstaps", [FD, GR_ON])).toBe("grstaps");
    expect(effectiveChatEngine("grstaps", [FD, GR_OFF])).toBe("fast_downward");
    expect(effectiveChatEngine("grstaps", [])).toBeUndefined(); // older backend: send nothing
    useChatEngine.setState({engine: "grstaps", engines: [FD, GR_ON]});
    expect(chatEngineNow()).toBe("grstaps");
  });

  it("switches and is remembered per browser", () => {
    useChatEngine.getState().setEngines([FD, GR_ON]);
    const el = render(<ChatEnginePicker />);
    const sel = el.querySelector<HTMLSelectElement>('[data-testid="chat-engine"]')!;
    expect(sel.value).toBe("fast_downward");
    expect([...sel.options].map(o => o.textContent)).toEqual(["Planner: Fast Downward", "Planner: GRSTAPS-X"]);
    act(() => {
      sel.value = "grstaps";
      sel.dispatchEvent(new Event("change", {bubbles: true}));
    });
    expect(useChatEngine.getState().engine).toBe("grstaps");
    expect(localStorage.getItem(CHAT_ENGINE_KEY)).toBe("grstaps");
    expect(chatEngineNow()).toBe("grstaps");
  });

  it("greys GRSTAPS-X out with the reason when the base does not run it", () => {
    useChatEngine.setState({engine: "grstaps", engines: [FD, GR_OFF]});
    const el = render(<ChatEnginePicker />);
    const sel = el.querySelector<HTMLSelectElement>('[data-testid="chat-engine"]')!;
    const gr = [...sel.options].find(o => o.value === "grstaps")!;
    expect(gr.disabled).toBe(true);
    expect(gr.title).toContain("no GRSTAPS-X planner is running");
    expect(sel.title).toContain("GRSTAPS-X is off: no GRSTAPS-X planner is running");
    expect(sel.value).toBe("fast_downward");
    expect(chatEngineNow()).toBe("fast_downward");
  });

  it("names the session's plan repair engine in the tooltip", () => {
    useChatEngine.setState({engine: "fast_downward", engines: [{...FD, repair: false}, {...GR_ON, repair: true}]});
    const el = render(<ChatEnginePicker />);
    const sel = el.querySelector<HTMLSelectElement>('[data-testid="chat-engine"]')!;
    expect(sel.title).toContain("Plan repair in this session: GRSTAPS-X");
    expect([...sel.options].find(o => o.value === "grstaps")!.title).toBe("plan repair in this session");
  });

  it("is hidden with Fast Downward alone (GRSTAPS-X not installed / not running), and a GRSTAPS-X pick sends Fast Downward", () => {
    useChatEngine.setState({engine: "grstaps", engines: [FD]});
    const el = render(<ChatEnginePicker />);
    expect(el.querySelector('[data-testid="chat-engine"]')).toBeNull();
    expect(chatEngineNow()).toBe("fast_downward");
  });

  it("is hidden without the backend's engine list", () => {
    const el = render(<ChatEnginePicker />);
    expect(el.querySelector('[data-testid="chat-engine"]')).toBeNull();
  });
});
