import { useEffect, useId, useRef, useState } from "react";

import { testAiConnection } from "../../api/aiConnectionTest";
import { ApiError } from "../../api/http";
import type { AiConnection, AiConnectionTestResult } from "../../api/types";
import { NOT_THE_WORKER, WEB_PROCESS_NOTE, describeTestError, describeTestResult } from "../../lib/aiConnectionTest";

/** One test at a time on the whole page, held by the page so two cards cannot start two. `begin` is synchronous, so a double click cannot start two requests. */
export interface TestLock {
  busyId: string | null;
  begin(connectionId: string): boolean;
  end(connectionId: string): void;
}

type TestState = { phase: "idle" } | { phase: "testing"; profileId: string } | { phase: "done"; result: AiConnectionTestResult } | { phase: "error"; error: unknown };

/**
 * The Test Connection action of one connection card. It tests the SAVED connection with one of its SAVED profiles (never the unsaved draft, never a connection that
 * has not been saved), asks the server once per click and never repeats a failed test by itself. Its state is React state only: nothing is stored in the browser, and
 * nothing here touches the settings draft, so unsaved edits are unaffected by any outcome. The server alone decides who may test and what the outcome is.
 */
export function ConnectionTestPanel({ saved, edited, lock }: { saved: AiConnection | undefined; edited: boolean; lock: TestLock }) {
  const selectId = useId();
  const [chosen, setChosen] = useState("");
  const [state, setState] = useState<TestState>({ phase: "idle" });
  const epoch = useRef(0);

  // A saved change (a new save, a refreshed status after a credential change) makes an older result or an in-flight answer stale: it is not shown any more.
  const savedKey = saved ? JSON.stringify(saved) : "";
  useEffect(() => {
    epoch.current += 1;
    setState({ phase: "idle" });
    return () => {
      epoch.current += 1;
    };
  }, [savedKey]);

  if (!saved) {
    return <p className="muted connection-test-hint">Save this connection before testing it. A test uses the saved connection only.</p>;
  }

  const profiles = saved.profiles;
  const profileId = profiles.some((p) => p.profile_id === chosen) ? chosen : (profiles[0]?.profile_id ?? "");
  const testing = state.phase === "testing";
  const running = lock.busyId !== null;
  const blocker = !saved.enabled
    ? "This connection is saved as disabled. Enable it and save the settings to test it."
    : profiles.length === 0
      ? "This connection has no saved model profile to test."
      : null;

  async function run() {
    if (!saved || blocker !== null || profileId === "" || !lock.begin(saved.connection_id)) return;
    epoch.current += 1;
    const mine = epoch.current;
    setState({ phase: "testing", profileId });
    try {
      const result = await testAiConnection(saved.connection_id, profileId);
      if (epoch.current === mine) setState({ phase: "done", result });
    } catch (error) {
      if (epoch.current === mine) setState({ phase: "error", error: error instanceof ApiError ? error : null });
    } finally {
      lock.end(saved.connection_id);
    }
  }

  const view = state.phase === "done" ? describeTestResult(state.result) : null;

  return (
    <div className="connection-test" aria-label={`Test ${saved.display_name}`} role="group">
      <div className="connection-test-controls">
        <div className="field">
          <label htmlFor={selectId}>Model profile to test</label>
          <select id={selectId} value={profileId} disabled={testing || profiles.length === 0} onChange={(event) => setChosen(event.target.value)}>
            {profiles.map((p) => (
              <option key={p.profile_id} value={p.profile_id}>
                {p.profile_id} — {p.model_id}
              </option>
            ))}
          </select>
        </div>
        <button
          type="button"
          className="button button-small"
          aria-label={`Test connection ${saved.display_name}`}
          disabled={running || blocker !== null || profileId === ""}
          onClick={() => void run()}
        >
          {testing ? "Testing…" : "Test connection"}
        </button>
      </div>

      <p className="muted connection-test-hint">{WEB_PROCESS_NOTE}</p>
      {blocker ? <p className="muted connection-test-hint">{blocker}</p> : null}
      {edited ? <p className="muted connection-test-hint">The test uses the saved version of this connection, not your unsaved edits.</p> : null}
      {running && !testing ? <p className="muted connection-test-hint">A connection test is still running. Wait for it to finish.</p> : null}

      <div role="status" aria-live="polite" className="connection-test-result">
        {testing ? <p>Testing {state.phase === "testing" ? state.profileId : ""}… this can take a few seconds. Do not leave the page.</p> : null}
        {view && state.phase === "done" ? (
          <div className={view.ok ? "test-result test-result-ok" : "test-result test-result-failed"}>
            <p>
              <span className={view.ok ? "badge badge-ok" : "badge badge-danger"}>{view.title}</span> <span className="muted">profile {state.result.profile_id}</span>
            </p>
            <dl className="connection-facts">
              <div>
                <dt>Duration</dt>
                <dd>{view.duration}</dd>
              </div>
              {view.code ? (
                <div>
                  <dt>Error code</dt>
                  <dd>
                    <code>{view.code}</code>
                  </dd>
                </div>
              ) : null}
            </dl>
            {view.detail ? <p>{view.detail}</p> : null}
            {!view.ok ? <p className="muted">{view.retryable ? "A later retry may help. Nothing is retried automatically; use Test connection to try again." : "Retrying is unlikely to help until the cause is fixed."}</p> : null}
            <p className="muted">{NOT_THE_WORKER}</p>
          </div>
        ) : null}
      </div>
      {state.phase === "error" ? (
        <p className="field-error" role="alert">
          {describeTestError(state.error)}
        </p>
      ) : null}
    </div>
  );
}
