// The chat's planner toggle: which engine the PDDL goals the chat proposes are
// planned with (Fast Downward or GRSTAPS-X). The backend gives the model that
// engine's goal rules (heracles_agents) and puts the engine on each goal card.
// Remembered per browser. GRSTAPS-X is offered only when the base runs it
// (GET /api/chat/usage `engines`, with the reason when it does not).
import {create} from "zustand";
import type {EngineId} from "./engines";

export interface ChatEngineInfo {
  id: EngineId;
  label: string;
  available: boolean;
  reason: string | null;
  /** plan repair (goal_manager) in this session forwards to this engine; the
   *  other engine's goals go to its planner directly (not repair-tracked) */
  repair?: boolean;
}

interface ChatEngineStore {
  /** the operator's pick (may be unavailable now: see effectiveChatEngine) */
  engine: EngineId;
  /** from the backend; empty = an older backend without the toggle */
  engines: ChatEngineInfo[];
  setEngine: (e: EngineId) => void;
  setEngines: (e: ChatEngineInfo[]) => void;
}

export const CHAT_ENGINE_KEY = "adt4.chat.engine";

function loadEngine(): EngineId {
  try {
    return localStorage.getItem(CHAT_ENGINE_KEY) === "grstaps" ? "grstaps" : "fast_downward";
  } catch {
    return "fast_downward";
  }
}

export const useChatEngine = create<ChatEngineStore>(set => ({
  engine: loadEngine(),
  engines: [],
  setEngine: engine => {
    try {
      localStorage.setItem(CHAT_ENGINE_KEY, engine);
    } catch {
      /* the choice just isn't remembered */
    }
    set({engine});
  },
  setEngines: engines => set({engines}),
}));

/** The engine the chat's goals use now: the pick when the base can run it, else
 *  Fast Downward; undefined when the backend has no toggle (send nothing). */
export function effectiveChatEngine(engine: EngineId, engines: ChatEngineInfo[]): EngineId | undefined {
  if (!engines.length) return undefined;
  return engines.find(e => e.id === engine)?.available ? engine : "fast_downward";
}

export function chatEngineNow(): EngineId | undefined {
  const {engine, engines} = useChatEngine.getState();
  return effectiveChatEngine(engine, engines);
}
