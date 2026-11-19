import { useEffect, useRef } from "react";

import { useToast } from "../components/ui/Toast";
import { isProcessingState } from "./format";

export interface TrackedRun {
  runId: string;
  reference: string;
  state: string;
}

/** A run that was still being processed and has now reached a state that needs a person's attention. */
export function isNotableTransition(previous: string | undefined, next: string): boolean {
  return previous !== undefined && isProcessingState(previous) && (next === "needs_review" || next === "failed");
}

/**
 * Tells the user (once) when a run they are looking at moves from processing to "needs review" or "failed". Only changes
 * seen by this screen count: the first data is a baseline, never a notification. The message carries the reference only,
 * never any request content.
 */
export function useStateTransitionToasts(runs: readonly TrackedRun[] | null): void {
  const { notify } = useToast();
  const previous = useRef<Map<string, string>>(new Map());
  const notified = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (runs === null) return;
    for (const run of runs) {
      const marker = `${run.runId}:${run.state}`;
      if (isNotableTransition(previous.current.get(run.runId), run.state) && !notified.current.has(marker)) {
        notified.current.add(marker);
        if (run.state === "needs_review") notify("info", `Request ${run.reference} is ready for review.`);
        else notify("error", `Request ${run.reference} failed during processing.`);
      }
      previous.current.set(run.runId, run.state);
    }
  }, [runs, notify]);
}
