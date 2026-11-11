// The chat header's planner toggle (Fast Downward | GRSTAPS-X): the engine of the
// PDDL goals the chat proposes from this turn on. GRSTAPS-X is greyed out, with
// the reason, when the base does not run it.
import {ENGINE_LABEL} from "../stores/engines";
import {effectiveChatEngine, useChatEngine} from "../stores/chatEngine";

export function ChatEnginePicker({onOpen}: {onOpen?: () => void}) {
  const {engine, engines, setEngine} = useChatEngine();
  if (!engines.length) return null;
  const now = effectiveChatEngine(engine, engines) ?? "fast_downward";
  const off = engines.find(e => !e.available);
  return (
    <select
      className="input input-sm"
      value={now}
      onChange={e => setEngine(e.target.value as typeof engine)}
      onFocus={onOpen}
      aria-label="Planner"
      data-testid="chat-engine"
      title={
        "Planner for the goals the chat proposes: the assistant writes goals for it and the goal cards send with it" +
        (off ? `. ${off.label} is off: ${off.reason}` : "")
      }
    >
      {engines.map(e => (
        <option key={e.id} value={e.id} disabled={!e.available} title={e.available ? undefined : (e.reason ?? undefined)}>
          {`Planner: ${ENGINE_LABEL[e.id] ?? e.label}${e.available ? "" : " (off)"}`}
        </option>
      ))}
    </select>
  );
}
