import { isRunState, stateLabel } from "../lib/format";

/** A run state as a coloured label. The text is always shown, so colour is never the only signal. */
export function StateBadge({ state }: { state: string }) {
  const known = isRunState(state) ? state : "unknown";
  return (
    <span className={`badge badge-${known}`} data-state={state}>
      <span className="badge-dot" aria-hidden="true" />
      {stateLabel(state)}
    </span>
  );
}
