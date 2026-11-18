// Penn's quad in the chat: the goal card, the arrival event (chat line, the
// capture in the chat) and the follow-up turn only the sending tab runs.
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {QuadGoalCard, toChatItem, type QuadGoalItem} from "../src/viewer/ChatPanel";
import {CLIENT_ID, followUpMessage, onQuadEvent, takeFollowUp, useQuad, type QuadGoal} from "../src/lib/quad";
import {useChat} from "../src/stores/chat";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLDivElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = host = null;
});
beforeEach(() => {
  useChat.setState({items: []});
  useQuad.setState({state: null, followUps: []});
});

const goal = (over: Partial<QuadGoal> = {}): QuadGoal => ({id: "g1", x: 4, y: 2, capture: true, query: "car", label: "O(3) car", status: "arrived", chat: {client: CLIENT_ID, follow_up: true}, ...over});
const capture = (id = "q2") => ({id, image_url: `api/quad/captures/${id}.jpg`, overlay_url: `api/quad/captures/${id}/overlay.jpg`, pose_map: {x: 4, y: 2}, segment: {query: "car", detections: [{label: "car", score: 0.9}]}});

describe("quad goal card", () => {
  it("parses the stream event and shows where, the picture and the SAM3 query", () => {
    const item = toChatItem({type: "quad_goal", x: 4, y: 2, label: "O(3) car", capture: true, query: "car", reason: "look for the car"}) as QuadGoalItem;
    expect(item.type).toBe("quad_goal");
    const onSend = vi.fn();
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root!.render(<QuadGoalCard it={item} onSend={onSend} />));
    expect(host.textContent).toContain("O(3) car");
    expect(host.textContent).toContain("takes a picture on arrival");
    expect(host.textContent).toContain("SAM3 looks for “car”");
    act(() => (host!.querySelector('[data-testid="quad-goal-send"]') as HTMLButtonElement).click());
    expect(onSend).toHaveBeenCalledOnce();
  });

  it("rejects a quad goal without a point", () => {
    expect(toChatItem({type: "quad_goal", label: "x"})?.type).toBe("error");
  });
});

describe("quad events", () => {
  it("a capture lands in the chat once and queues the follow-up for the sending tab", () => {
    const ev = {kind: "captured", t: 1, goal: goal(), capture: capture()};
    onQuadEvent(ev);
    onQuadEvent(ev); // a replay (reconnect) adds nothing
    const imgs = useChat.getState().items.filter(i => i.type === "images");
    expect(imgs).toHaveLength(1);
    expect(imgs[0].type === "images" && imgs[0].items[0].images[0]).toBe("api/quad/captures/q2/overlay.jpg");
    const f = takeFollowUp();
    expect(f?.attach).toEqual({quad_capture: "q2"});
    expect(f?.message).toContain("capture q2");
    expect(takeFollowUp()).toBeNull();
  });

  it("another tab's goal shows the capture but runs no follow-up", () => {
    onQuadEvent({kind: "captured", t: 1, goal: goal({chat: {client: "other-tab", follow_up: true}}), capture: capture("q3")});
    expect(useChat.getState().items.some(i => i.type === "images")).toBe(true);
    expect(takeFollowUp()).toBeNull();
  });

  it("arrival and failure become goal event lines", () => {
    onQuadEvent({kind: "arrived", t: 2, goal: goal({id: "g9"})});
    onQuadEvent({kind: "failed", t: 3, goal: goal({id: "g9", status: "failed", error: "timed out"})});
    const lines = useChat.getState().items.filter(i => i.type === "goal_event");
    expect(lines.map(l => l.type === "goal_event" && l.event.stage)).toEqual(["done", "failed"]);
  });

  it("the follow-up message names a failed SAM3 run", () => {
    const m = followUpMessage(goal(), {...capture(), segment: {query: "car", error: "SAM3 down"}});
    expect(m).toContain("SAM3 on 'car' failed");
  });
});
