// Toasts for "a robot's part of a goal ended" (lib/goalEvents), shown on every
// page: a robot reaching its goal is news wherever the operator is looking.
import {useEffect} from "react";
import {dismissGoalEvent, eventLine, listenGoalEvents, useGoalEvents} from "../lib/goalEvents";

export function GoalEventToasts() {
  useEffect(() => listenGoalEvents(), []);
  const toasts = useGoalEvents(s => s.toasts);
  if (!toasts.length) return null;
  return (
    <div className="goal-event-toasts" aria-live="polite">
      {toasts.map(t => {
        const tone = t.stage === "done" ? "ok" : t.stage === "failed" ? "err" : "warn";
        return (
          <div key={t.key} className={`goal-event-toast tone-border-${tone}`} role={t.stage === "failed" ? "alert" : "status"} data-testid="goal-event-toast">
            <div className="goal-event-toast-text">
              <strong>{eventLine(t)}</strong>
              {t.text && <div className="small muted">{t.text}</div>}
            </div>
            <button type="button" className="icon-btn" aria-label="Dismiss" onClick={() => dismissGoalEvent(t.key)}>
              ×
            </button>
          </div>
        );
      })}
    </div>
  );
}
