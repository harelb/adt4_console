import {afterEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {ChatPanel} from "../src/viewer/ChatPanel";
import {clearChat, setChatDraft, useChat} from "../src/stores/chat";
import {useViewer} from "../src/stores/viewer";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

// the side panel mounts only the docked tab: a click on a node selects it and
// brings Inspect forward, which unmounts the Chat tab (ViewerPage's onNode)
describe("the unsent chat draft survives inspecting", () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    root = null;
    vi.restoreAllMocks();
    clearChat();
    setChatDraft("");
    useViewer.getState().select(null);
  });

  const mount = async (el = <ChatPanel />) => {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root!.render(el));
  };
  const box = () => host!.querySelector<HTMLTextAreaElement>("textarea[aria-label='Chat message']")!;
  const type = async (text: string) => {
    const ta = box();
    const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
    await act(async () => {
      set.call(ta, text);
      ta.dispatchEvent(new Event("input", {bubbles: true}));
    });
  };

  it("keeps the text when a node is selected and the Chat tab unmounts", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", {status: 200})));
    await mount();
    await type("go to the red car and");
    expect(box().value).toBe("go to the red car and");
    // inspect: select + the tab switch unmounts the chat
    act(() => useViewer.getState().select({src: "neo4j:live", sym: "t1242"}));
    act(() => root!.unmount());
    host!.remove();
    // back to Chat
    await mount();
    expect(box().value).toBe("go to the red car and");
    // the mission planner's chat pane is the same conversation: same draft
    act(() => root!.unmount());
    host!.remove();
    await mount(<ChatPanel missionMode />);
    expect(box().value).toBe("go to the red car and");
  });

  it("does not take focus when it mounts with a draft", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", {status: 200})));
    setChatDraft("half a sentence");
    const other = document.createElement("input");
    document.body.appendChild(other);
    other.focus();
    await mount();
    expect(box().value).toBe("half a sentence");
    expect(document.activeElement).toBe(other);
    other.remove();
  });

  it("sending still clears the box", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", {status: 200})));
    await mount();
    await type("hello");
    const send = [...host!.querySelectorAll("button")].find(b => b.textContent === "Send")!;
    await act(async () => send.click());
    expect(box().value).toBe("");
    expect(useChat.getState().draft).toBe("");
  });
});
