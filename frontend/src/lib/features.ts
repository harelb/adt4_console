// Optional server features (GET /api/health `features`, the backend's
// optional.FEATURES): a workspace without a package turns its feature off and
// the UI hides it. Unknown (an older backend, health not loaded yet) = on.
import {useFleet} from "../stores/fleet";
import type {Health} from "../types";

export type Feature =
  | "fleet_core"
  | "fleetlink"
  | "map_edit"
  | "scene_edit_tools"
  | "pddl_planning"
  | "grstaps"
  | "plan_validity"
  | "image_store"
  | "action_results"
  | "lease_state";

export function featureOn(health: Health | null | undefined, f: Feature): boolean {
  return health?.features?.[f] !== false;
}

export function useFeature(f: Feature): boolean {
  return useFleet(s => featureOn(s.health, f));
}

/** Whether ``robot``'s executor serves ~/take_lease (unknown = yes). */
export function takeLeaseOn(health: Health | null | undefined, robot: string): boolean {
  return health?.take_lease?.[robot] !== false;
}
