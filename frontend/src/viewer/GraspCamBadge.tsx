// "hand camera · grasp in progress" strip over a camera view while the grasp
// override (stores/graspCamera.ts) shows the hand camera; "back" restores the
// operator's previous camera at once.
import {useEffect, useState} from "react";
import {useGraspCamera, type GraspSession} from "../stores/graspCamera";

export function GraspCamBadge({session, className = ""}: {session: GraspSession; className?: string}) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);
  const left = Math.max(0, Math.ceil((session.deadline - now) / 1000));
  return (
    <div className={`grasp-cam-badge row gap-sm ${className}`} role="status" data-testid="grasp-cam-badge">
      <span className="grasp-cam-dot" aria-hidden="true" />
      <span className="small">
        hand camera · grasp in progress
        <span className="muted"> · {left} s</span>
      </span>
      <button type="button" className="btn btn-xs" onClick={() => useGraspCamera.getState().end("back")} title="Back to the previous camera now">
        back
      </button>
    </div>
  );
}

/** The per-browser setting. */
export function GraspCamToggle() {
  const enabled = useGraspCamera(s => s.enabled);
  const setEnabled = useGraspCamera(s => s.setEnabled);
  return (
    <label className="grasp-cam-toggle small" title="Show the robot's hand camera as live video after a grasp is approved, until the grasp is done (remembered in this browser)">
      <input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} /> Auto hand camera during grasp
    </label>
  );
}
