// The robot legend's relocalization chip, clicked: the evidence behind the
// verdict in one place — hydra_multi's accepted prior-map loop closures (the
// relocalized rule: at least RELOC_MIN_HYDRA_LCS), fleetlink's ROMAN counter and
// the robot's roman_lc row, and hydra_multi's last optimization (how long ago,
// ticking, and how long it took; from its status note).
import {useEffect, useState} from "react";
import {useRobots} from "../stores/robots";
import {hydraMatches, hydraOptimization, RELOC_MIN_HYDRA_LCS, type RelocContext} from "../lib/frameAlign";

export function fmtAgo(s: number): string {
  if (s < 90) return `${s.toFixed(0)} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  return `${(s / 3600).toFixed(1)} h ago`;
}

export function fmtTook(s: number): string {
  return s < 1 ? `${Math.round(s * 1000)} ms` : `${s.toFixed(2)} s`;
}

export function RelocPopover({robot, ctx, onClose}: {robot: string; ctx: RelocContext; onClose: () => void}) {
  const reloc = useRobots(s => s.state[robot]?.reloc ?? s.link[robot]?.reloc);
  const at = useRobots(s => s.stateAt[robot]);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => {
      window.clearInterval(t);
      window.removeEventListener("keydown", onKey);
    };
  }, [onClose]);
  const hm = hydraMatches(ctx.hydraMulti);
  const opt = hydraOptimization(ctx.hydraMulti);
  const since = at ? Math.max(0, (now - at) / 1000) : 0;
  const need = RELOC_MIN_HYDRA_LCS;
  const hydraText = hm
    ? `${hm.accepted} accepted / ${hm.total} total`
    : ctx.hydraMulti
      ? `no count (${ctx.hydraMulti.status ?? "?"}${ctx.hydraMulti.notes ? `: ${ctx.hydraMulti.notes}` : ""})`
      : ctx.hydraMulti === null
        ? "not in the robot's status table"
        : "no status table from the robot";
  const romanText = reloc ? `${reloc.prior_lcs} prior-map matches seen by fleetlink` : "no ROMAN counter from fleetlink";
  const optText = opt
    ? null
    : /no optimization yet/i.test(ctx.hydraMulti?.notes ?? "")
      ? "none yet"
      : "not reported (hydra_multi without optimization timing)";
  return (
    <div
      className="reloc-pop"
      role="dialog"
      aria-label={`${robot} localization`}
      data-testid={`reloc-pop-${robot}`}
      onClick={e => e.stopPropagation()}
      onKeyDown={e => e.stopPropagation()}
    >
      <div className="reloc-pop-head">
        <strong>{robot} localization</strong>
        <button type="button" className="btn btn-xs" aria-label="Close" onClick={onClose}>
          ×
        </button>
      </div>
      <dl className="reloc-pop-grid">
        <dt>Hydra loop closures</dt>
        <dd data-testid={`reloc-pop-hydra-${robot}`}>
          {hydraText}
          <div className="muted">relocalized at {need} accepted</div>
        </dd>
        <dt>ROMAN loop closures</dt>
        <dd data-testid={`reloc-pop-roman-${robot}`}>
          {romanText}
          {ctx.roman ? (
            <div className="muted">
              roman_lc {ctx.roman.status ?? "?"}
              {ctx.roman.notes ? ` — ${ctx.roman.notes}` : ""}
            </div>
          ) : null}
        </dd>
        <dt>Last Hydra optimization</dt>
        <dd data-testid={`reloc-pop-opt-${robot}`}>
          {opt ? (
            <>
              {fmtAgo(opt.agoS + since)} · took {fmtTook(opt.tookS)}
            </>
          ) : (
            optText
          )}
        </dd>
      </dl>
    </div>
  );
}
