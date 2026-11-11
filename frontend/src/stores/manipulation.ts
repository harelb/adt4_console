// Pending manipulation (grasp) approvals, shared by the global approval host
// (which polls them) and the viewer (toolbar badge, robot legend chips). A
// request never opens the panel by itself: the operator opens it from a badge;
// the robot waits until it is answered.
import {create} from "zustand";

export interface PendingGrasp {
  id: string;
  robot: string;
  simulated?: boolean;
}

interface ManipulationStore {
  /** unanswered requests, oldest first */
  pending: PendingGrasp[];
  /** the request whose panel is open (null: closed) */
  openId: string | null;
  /** viewer badges mounted (the global floating button hides while > 0) */
  badges: number;
  addBadge: (d: number) => void;
  setPending: (p: PendingGrasp[]) => void;
  /** open a request's panel: by id, the robot's oldest, or the oldest of all */
  open: (by?: {id?: string; robot?: string}) => void;
  close: () => void;
}

export const useManipulation = create<ManipulationStore>((set, get) => ({
  pending: [],
  openId: null,
  badges: 0,
  addBadge: d => set(s => ({badges: Math.max(0, s.badges + d)})),
  setPending: pending => {
    const same = pending.length === get().pending.length && pending.every((p, i) => p.id === get().pending[i]?.id);
    if (!same) set({pending});
  },
  open: by => {
    const list = get().pending;
    const hit = by?.id ? list.find(p => p.id === by.id) : by?.robot ? list.find(p => p.robot === by.robot) : list[0];
    set({openId: hit?.id ?? by?.id ?? null});
  },
  close: () => set({openId: null}),
}));

/** "Grasp approval: spot1 — open" / "Grasp approvals (3): spot1, spot2 — open" */
export function pendingLabel(pending: PendingGrasp[]): string {
  if (!pending.length) return "";
  const robots = [...new Set(pending.map(p => p.robot))];
  return pending.length === 1 ? `Grasp approval: ${robots[0]} — open` : `Grasp approvals (${pending.length}): ${robots.join(", ")} — open`;
}
