import { useMemo, useRef, useState, type FormEvent } from "react";

import { getAiSettings, saveAiSettings } from "../api/aiSettings";
import { ApiError } from "../api/http";
import type { AiSettings } from "../api/types";
import { ConnectionCard } from "../components/ai/ConnectionCard";
import { CredentialsPanel } from "../components/ai/CredentialsPanel";
import { ConfirmStep } from "../components/ConfirmStep";
import { PageHeader } from "../components/PageHeader";
import { ErrorState, LoadingState, Notice } from "../components/Status";
import { Icon } from "../components/ui/Icon";
import { useToast } from "../components/ui/Toast";
import {
  MAX_CONNECTIONS,
  SOURCE_NONE,
  buildUpdate,
  connectionSource,
  describeCodes,
  describeSettingsCode,
  describeStatus,
  draftFromSettings,
  emptyConnection,
  isDirty,
  legacySource,
  localityLabel,
  parseConnectionPath,
  permittedPolicyModes,
  policyHelp,
  policyLabel,
  profileOptions,
  providerTypeLabel,
  rebaseDraft,
  selectedConnection,
  selectedLegacyId,
  selectedLocality,
  validateDraft,
  type ConnectionDraft,
  type Mode,
  type Problems,
  type SettingsDraft,
} from "../lib/aiSettingsForm";
import { describeError, fieldCodesFrom, isPermissionError, isStaleSettings } from "../lib/errors";
import { formatDateTime } from "../lib/format";
import { useResource } from "../lib/useResource";

const SETTINGS_FIELDS = ["extraction_mode", "connection_id", "provider_id", "profile_id", "policy_mode", "timeout_seconds"] as const;

type Confirm = { kind: "disable" | "remove"; key: string } | { kind: "discard" } | { kind: "cloud" } | null;

function mergeProblems(...sources: Problems[]): Problems {
  const merged: Problems = {};
  for (const source of sources) {
    for (const [path, codes] of Object.entries(source)) {
      const existing = merged[path] ?? [];
      merged[path] = [...existing, ...codes.filter((code) => !existing.includes(code))];
    }
  }
  return merged;
}

function FieldMessage({ id, codes }: { id: string; codes: string[] | undefined }) {
  const text = describeCodes(codes);
  return text ? (
    <p id={id} className="field-error">
      {text}
    </p>
  ) : null;
}

function Editor({ initial, onReload }: { initial: AiSettings; onReload: () => void }) {
  const { notify } = useToast();
  const [baseline, setBaseline] = useState<AiSettings>(initial);
  const [draft, setDraft] = useState<SettingsDraft>(() => draftFromSettings(initial));
  const [openKey, setOpenKey] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<Confirm>(null);
  const [attempted, setAttempted] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<ApiError | null>(null);
  const [serverProblems, setServerProblems] = useState<Problems>({});
  const [conflict, setConflict] = useState<{ latest: AiSettings | null } | null>(null);
  const [restartRevision, setRestartRevision] = useState<number | null>(null);
  const [rebased, setRebased] = useState<number | null>(null);
  const inFlight = useRef(false);

  const catalog = baseline.catalog;
  const dirty = isDirty(draft, baseline);
  const clientProblems = useMemo(() => validateDraft(draft, catalog), [draft, catalog]);
  const problems = attempted ? mergeProblems(clientProblems, serverProblems) : serverProblems;

  const connection = selectedConnection(draft);
  const legacyId = selectedLegacyId(draft);
  const active = draft.mode === "ai" ? connection : null;
  const permitted = permittedPolicyModes(draft, catalog);
  const profiles = profileOptions(draft, catalog);
  const locality = selectedLocality(draft, catalog);
  const atLimit = draft.connections.length >= MAX_CONNECTIONS;

  const savedSelection = connection && !connection.isNew ? baseline.connections.find((c) => c.connection_id === connection.connection_id) : undefined;
  const selectedStatus = draft.mode === "ai" ? (savedSelection?.status ?? (legacyId ? catalog.providers.find((p) => p.provider_id === legacyId)?.status : undefined)) : undefined;

  function edit(next: SettingsDraft) {
    setDraft(next);
  }

  function setMode(mode: Mode) {
    edit({ ...draft, mode });
  }

  function setSource(source: string) {
    const next = { ...draft, source };
    const offered = profileOptions(next, catalog);
    edit({ ...next, profile_id: offered.some((p) => p.profile_id === draft.profile_id) ? draft.profile_id : "" });
  }

  function updateConnection(index: number, next: ConnectionDraft) {
    const previous = draft.connections[index];
    if (!previous) return;
    const connections = draft.connections.map((c, i) => (i === index ? next : c));
    // The selection names a connection by identifier; renaming a new connection keeps it selected.
    let { source, profile_id } = draft;
    if (source === connectionSource(previous.connection_id)) {
      source = connectionSource(next.connection_id);
      if (!next.profiles.some((p) => p.profile_id === profile_id)) profile_id = "";
    }
    edit({ ...draft, connections, source, profile_id });
  }

  function addConnection() {
    if (atLimit) return;
    const fresh = emptyConnection(catalog);
    edit({ ...draft, connections: [...draft.connections, fresh] });
    setOpenKey(fresh.key);
  }

  function removeConnection(key: string) {
    edit({ ...draft, connections: draft.connections.filter((c) => c.key !== key) });
    if (openKey === key) setOpenKey(null);
  }

  function discard() {
    setDraft(draftFromSettings(baseline));
    setOpenKey(null);
    setAttempted(false);
    setSaveError(null);
    setServerProblems({});
    setConflict(null);
    setRebased(null);
  }

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    if (inFlight.current) return;
    setAttempted(true);
    setRebased(null);
    const found = validateDraft(draft, catalog);
    if (Object.keys(found).length > 0) {
      setSaveError(null);
      const first = Object.keys(found).map(parseConnectionPath).find((p) => p !== null);
      if (first) setOpenKey(draft.connections[first.index]?.key ?? null);
      return;
    }
    const selectedCloud = draft.mode === "ai" && locality === "cloud";
    const wasCloud = baseline.settings.extraction_mode === "ai" && baseline.settings.connection_id !== null && baseline.connections.find((c) => c.connection_id === baseline.settings.connection_id)?.locality === "cloud";
    if (selectedCloud && !(wasCloud && baseline.settings.connection_id === connection?.connection_id)) {
      setConfirm({ kind: "cloud" });
      return;
    }
    await persist();
  }

  async function persist() {
    if (inFlight.current) return;
    inFlight.current = true;
    setConfirm(null);
    setSaving(true);
    setSaveError(null);
    setServerProblems({});
    setConflict(null);
    try {
      const saved = await saveAiSettings(buildUpdate(draft, baseline));
      setBaseline(saved);
      setDraft(draftFromSettings(saved));
      setAttempted(false);
      setOpenKey(null);
      setRestartRevision(saved.revision);
      notify("success", "AI settings saved.");
    } catch (error) {
      const failure = error instanceof ApiError ? error : new ApiError(0, "unexpected_response", "");
      setSaveError(failure);
      if (failure.code === "validation_error") {
        const fields = fieldCodesFrom(failure);
        setServerProblems(fields);
        const first = Object.keys(fields).map(parseConnectionPath).find((p) => p !== null);
        if (first) setOpenKey(draft.connections[first.index]?.key ?? null);
      } else if (isStaleSettings(failure)) {
        // Fetch what the other administrator saved so the user can choose; the edits stay in the form either way.
        try {
          setConflict({ latest: await getAiSettings() });
        } catch {
          setConflict({ latest: null });
        }
      }
    } finally {
      inFlight.current = false;
      setSaving(false);
    }
  }

  function keepMyEdits(latest: AiSettings) {
    setDraft(rebaseDraft(draft, baseline, latest));
    setBaseline(latest);
    setConflict(null);
    setSaveError(null);
    setServerProblems({});
    setRebased(latest.revision);
  }

  function useLatest(latest: AiSettings) {
    setBaseline(latest);
    setDraft(draftFromSettings(latest));
    setOpenKey(null);
    setAttempted(false);
    setConflict(null);
    setSaveError(null);
    setServerProblems({});
  }

  async function refreshStatuses() {
    // A credential change alters only what the server computes for a connection (its status), never its stored content. Update the statuses
    // only when the settings are the very revision on screen, so no other change is pulled in and no unsaved edit is touched.
    try {
      const latest = await getAiSettings();
      setBaseline((current) =>
        latest.revision !== current.revision
          ? current
          : { ...current, connections: current.connections.map((c) => ({ ...c, status: latest.connections.find((l) => l.connection_id === c.connection_id)?.status ?? c.status })) },
      );
    } catch {
      // The statuses stay as they were; "Check status" in the credentials section is the reliable view.
    }
  }

  const shownSettingsPaths = new Set<string>([...SETTINGS_FIELDS, "connections"]);
  const unplaced = Object.entries(problems).filter(([path]) => !shownSettingsPaths.has(path) && parseConnectionPath(path) === null);
  const stale = saveError !== null && isStaleSettings(saveError);
  const otherError = saveError !== null && !stale && saveError.code !== "validation_error";

  return (
    <div className="ai-page">
    <form className="ai-settings" onSubmit={submit} noValidate>
      {restartRevision !== null ? (
        <Notice kind="warning">
          <strong>Worker restart required.</strong> Revision {restartRevision} is saved, but a running worker keeps using the settings it started with. The new settings are not active until the worker is
          restarted. This page cannot restart it; ask the operator to stop and start the worker (see the runbook, section 5.10).
        </Notice>
      ) : null}

      <Notice kind="info">
        Saved changes reach the worker only when it starts. There is no automatic fallback: if the selected connection or model cannot be used, the worker does not start and requests stay queued. Nothing is
        downloaded automatically. A secret is never part of these settings: it is entered only in the Managed credentials section below, is never shown again, and is never stored in the browser.
      </Notice>

      {baseline.unreadable_connections > 0 ? (
        <Notice kind="warning">
          {baseline.unreadable_connections} stored connection{baseline.unreadable_connections === 1 ? "" : "s"} could not be read and {baseline.unreadable_connections === 1 ? "is" : "are"} not listed. They are kept unless you
          change the connection list; saving a changed list replaces it.
        </Notice>
      ) : null}

      {stale ? (
        <Notice kind="warning">
          <strong>The settings were changed by someone else.</strong> Nothing was saved, and your edits are still here.
          {conflict?.latest ? ` The latest settings are revision ${conflict.latest.revision}.` : null}
          <div className="form-actions notice-actions">
            {conflict?.latest ? (
              <button type="button" className="button button-primary" onClick={() => keepMyEdits(conflict.latest as AiSettings)}>
                Keep my edits on top of the latest
              </button>
            ) : null}
            {conflict?.latest ? (
              <button type="button" className="button" onClick={() => useLatest(conflict.latest as AiSettings)}>
                Discard my edits and load the latest
              </button>
            ) : (
              <button type="button" className="button" onClick={onReload}>
                Discard my edits and reload
              </button>
            )}
          </div>
        </Notice>
      ) : null}
      {rebased !== null ? (
        <Notice kind="info">Your edits are now based on revision {rebased}. Review them, then save.</Notice>
      ) : null}
      {saveError?.code === "validation_error" ? <Notice kind="error">Some values are not valid, so nothing was saved. Your edits are still here; the problems are shown beside the fields.</Notice> : null}
      {otherError ? <Notice kind="error">{describeError(saveError)} Nothing was saved, and your edits are still here.</Notice> : null}
      {unplaced.length > 0 ? (
        <Notice kind="error">
          <ul className="plain-list">
            {unplaced.map(([path, codes]) => (
              <li key={path}>
                {describeCodes(codes) ?? describeSettingsCode("invalid_value")}
              </li>
            ))}
          </ul>
        </Notice>
      ) : null}

      <section className="card" aria-labelledby="ai-extraction">
        <div className="section-head">
          <h2 id="ai-extraction">Extraction</h2>
        </div>
        <fieldset className="mode-set">
          <legend>Extraction mode</legend>
          <label className="radio-row">
            <input type="radio" name="extraction_mode" value="deterministic" checked={draft.mode === "deterministic"} onChange={() => setMode("deterministic")} />
            <span>
              Deterministic parser <span className="muted">— the default. A fixed, labelled-line parser; no AI model and no network call.</span>
            </span>
          </label>
          <label className="radio-row">
            <input type="radio" name="extraction_mode" value="ai" checked={draft.mode === "ai"} onChange={() => setMode("ai")} />
            <span>
              AI extraction <span className="muted">— a model proposes the draft. Every result is still validated and goes through human review.</span>
            </span>
          </label>
        </fieldset>
        <FieldMessage id="mode-problem" codes={problems.extraction_mode} />

        {draft.mode === "ai" ? (
          <div className="ai-fields">
            <div className="field-row">
              <div className="field">
                <label htmlFor="ai-source">Active connection</label>
                <select
                  id="ai-source"
                  value={draft.source}
                  aria-invalid={problems.connection_id || problems.provider_id ? true : undefined}
                  aria-describedby="source-problem"
                  onChange={(e) => setSource(e.target.value)}
                >
                  <option value={SOURCE_NONE}>Choose a connection…</option>
                  {draft.connections.length > 0 ? (
                    <optgroup label="Connections">
                      {draft.connections.map((c) => (
                        <option key={c.key} value={connectionSource(c.connection_id)} disabled={!c.enabled || c.connection_id.trim() === ""}>
                          {(c.display_name.trim() || c.connection_id || "New connection") + " — " + providerTypeLabel(catalog, c.provider_type)}
                          {c.isNew ? " (unsaved)" : ""}
                          {c.enabled ? "" : " (disabled)"}
                        </option>
                      ))}
                    </optgroup>
                  ) : null}
                  {catalog.providers.length > 0 ? (
                    <optgroup label="Environment-configured provider">
                      {catalog.providers.map((p) => (
                        <option key={p.provider_id} value={legacySource(p.provider_id)}>
                          {p.label} (set in the backend environment)
                        </option>
                      ))}
                    </optgroup>
                  ) : null}
                </select>
                <div id="source-problem">
                  <FieldMessage id="source-problem-text" codes={[...(problems.connection_id ?? []), ...(problems.provider_id ?? [])]} />
                </div>
              </div>

              <div className="field">
                <label htmlFor="ai-profile">Model profile</label>
                <select
                  id="ai-profile"
                  value={draft.profile_id}
                  disabled={draft.source === SOURCE_NONE}
                  aria-invalid={problems.profile_id ? true : undefined}
                  aria-describedby="profile-problem"
                  onChange={(e) => edit({ ...draft, profile_id: e.target.value })}
                >
                  <option value="">{draft.source === SOURCE_NONE ? "Choose a connection first" : "Choose a model profile…"}</option>
                  {profiles.map((p) => (
                    <option key={p.profile_id} value={p.profile_id}>
                      {p.profile_id} — {p.model_id}
                    </option>
                  ))}
                </select>
                <p className="field-hint">Choosing a model is always explicit. There is no automatic fallback to another model.</p>
                <div id="profile-problem">
                  <FieldMessage id="profile-problem-text" codes={problems.profile_id} />
                </div>
              </div>
            </div>

            <div className="field-row">
              <div className="field">
                <label htmlFor="ai-policy">Data locality policy</label>
                <select
                  id="ai-policy"
                  value={draft.policy_mode}
                  aria-invalid={problems.policy_mode ? true : undefined}
                  aria-describedby="policy-help policy-problem"
                  onChange={(e) => edit({ ...draft, policy_mode: e.target.value })}
                >
                  <option value="">Choose a policy…</option>
                  {catalog.policy_modes.map((mode) => {
                    const allowed = permitted === null || permitted.includes(mode);
                    return (
                      <option key={mode} value={mode} disabled={!allowed}>
                        {policyLabel(mode)}
                        {allowed ? "" : " (not permitted for this connection)"}
                      </option>
                    );
                  })}
                </select>
                <p id="policy-help" className="field-hint">
                  {(draft.policy_mode && policyHelp(draft.policy_mode)) ?? "Where request text may be sent. The gateway checks this on every call."}
                </p>
                <div id="policy-problem">
                  <FieldMessage id="policy-problem-text" codes={problems.policy_mode} />
                </div>
              </div>

              <div className="field">
                <label htmlFor="ai-timeout">Timeout (seconds)</label>
                <input
                  id="ai-timeout"
                  type="text"
                  inputMode="numeric"
                  autoComplete="off"
                  value={draft.timeout}
                  aria-invalid={problems.timeout_seconds ? true : undefined}
                  aria-describedby="timeout-help timeout-problem"
                  onChange={(e) => edit({ ...draft, timeout: e.target.value })}
                />
                <p id="timeout-help" className="field-hint">
                  Between {catalog.timeout.min} and {catalog.timeout.max} seconds; the default is {catalog.timeout.default}. The worker refuses to start if its lease cannot cover this.
                </p>
                <div id="timeout-problem">
                  <FieldMessage id="timeout-problem-text" codes={problems.timeout_seconds} />
                </div>
              </div>
            </div>

            {locality ? (
              <p className="muted">
                Selected data locality: <strong>{localityLabel(locality)}</strong>.
              </p>
            ) : null}
            {locality === "cloud" ? (
              <Notice kind="warning">
                This connection sends request text to a <strong>cloud</strong> provider. It can run only under the Approved cloud policy, and choosing it here is your explicit approval.
              </Notice>
            ) : null}
            {selectedStatus && !selectedStatus.configured ? (
              <Notice kind="warning">
                The selected connection is <strong>not configured</strong>: {describeStatus(selectedStatus, savedSelection?.credential_source).detail} If the backend environment is not fixed, the worker will not start and requests will stay queued.
              </Notice>
            ) : null}
          </div>
        ) : null}
      </section>

      <section className="card" aria-labelledby="ai-connections">
        <div className="section-head">
          <h2 id="ai-connections">Connections</h2>
          <span className="muted" aria-live="polite">
            {draft.connections.length} of {MAX_CONNECTIONS}
          </span>
        </div>
        <p className="muted">
          A connection names one provider, where it runs, the models it may use and the <em>name</em> of its credential and whether that credential is an environment variable set by the operator or a
          managed credential stored by the server (see Managed credentials below).
        </p>
        <FieldMessage id="connections-problem" codes={problems.connections} />

        {draft.connections.length === 0 ? (
          <div className="status status-empty">
            <Icon name="inbox" />
            <p className="empty-title">No connections yet</p>
            <p>Without a connection, AI extraction can use only the environment-configured provider.</p>
          </div>
        ) : (
          <ul className="connection-list">
            {draft.connections.map((c, index) => {
              const own: Record<string, string[]> = {};
              for (const [path, codes] of Object.entries(problems)) {
                const parsed = parseConnectionPath(path);
                if (parsed && parsed.index === index) own[parsed.field] = codes;
              }
              const askKind = confirm && "key" in confirm && confirm.key === c.key ? confirm.kind : null;
              return (
                <ConnectionCard
                  key={c.key}
                  draft={c}
                  catalog={catalog}
                  saved={c.isNew ? undefined : baseline.connections.find((s) => s.connection_id === c.connection_id)}
                  problems={own}
                  active={active !== null && active.key === c.key}
                  open={openKey === c.key}
                  confirm={askKind}
                  onOpen={() => setOpenKey(c.key)}
                  onClose={() => setOpenKey(null)}
                  onChange={(next) => updateConnection(index, next)}
                  onAskDisable={() => setConfirm({ kind: "disable", key: c.key })}
                  onAskRemove={() => (c.isNew ? removeConnection(c.key) : setConfirm({ kind: "remove", key: c.key }))}
                  onEnable={() => updateConnection(index, { ...c, enabled: true })}
                  onConfirm={() => {
                    if (askKind === "remove") removeConnection(c.key);
                    else updateConnection(index, { ...c, enabled: false });
                    setConfirm(null);
                  }}
                  onCancelConfirm={() => setConfirm(null)}
                />
              );
            })}
          </ul>
        )}

        <div className="form-actions">
          <button type="button" className="button" disabled={atLimit} onClick={addConnection}>
            <Icon name="plus" />
            Add connection
          </button>
          {atLimit ? <span className="muted">The maximum of {MAX_CONNECTIONS} connections is reached. Remove one to add another.</span> : null}
        </div>
      </section>

      <section className="card save-bar" aria-label="Save">
        {confirm?.kind === "cloud" ? (
          <ConfirmStep title="Approve sending request text to a cloud provider?" confirmLabel="Approve and save" busy={saving} onConfirm={persist} onCancel={() => setConfirm(null)}>
            <p>
              After the worker restarts, request text will leave the company for <strong>{connection?.display_name.trim() || "this connection"}</strong>. Saving records you as having approved this.
            </p>
          </ConfirmStep>
        ) : null}
        {confirm?.kind === "discard" ? (
          <ConfirmStep title="Discard your unsaved changes?" confirmLabel="Discard changes" danger busy={false} onConfirm={() => { discard(); setConfirm(null); }} onCancel={() => setConfirm(null)}>
            <p>The form returns to the last saved settings (revision {baseline.revision}).</p>
          </ConfirmStep>
        ) : null}
        {confirm === null || (confirm.kind !== "cloud" && confirm.kind !== "discard") ? (
          <div className="form-actions">
            <button type="submit" className="button button-primary" disabled={saving || !dirty}>
              {saving ? "Saving…" : "Save settings"}
            </button>
            <button type="button" className="button" disabled={saving || !dirty} onClick={() => setConfirm({ kind: "discard" })}>
              Discard changes
            </button>
            <span className="muted" role="status">
              {dirty ? "You have unsaved changes." : "No unsaved changes."}
            </span>
          </div>
        ) : null}
        <p className="muted save-meta">
          {baseline.stored ? (
            <>
              Revision {baseline.revision}
              {baseline.updated_at ? `, saved ${formatDateTime(baseline.updated_at)}` : ""}.
            </>
          ) : (
            "No settings have been saved yet; the default is the deterministic parser."
          )}{" "}
          Every save is recorded in the audit log.
        </p>
      </section>
    </form>
    <CredentialsPanel connections={baseline.connections} prefix={catalog.credential_ref_prefix} onChanged={() => void refreshStatuses()} />
    </div>
  );
}

export function AiSettingsPage() {
  const { state, reload } = useResource(getAiSettings, []);
  return (
    <section>
      <PageHeader
        title="AI settings"
        breadcrumbs={[{ label: "Settings" }, { label: "AI settings" }]}
        description="How Request Intake extracts a request: the mode, the named AI connections, the model and the data policy. Administrators only."
      />
      {state.status === "loading" ? <LoadingState label="Loading AI settings…" skeleton={3} /> : null}
      {state.status === "error" ? (
        <ErrorState
          title={isPermissionError(state.error) ? "Not available for your account" : "Could not load the AI settings"}
          message={describeError(state.error)}
          onRetry={isPermissionError(state.error) ? undefined : reload}
        />
      ) : null}
      {state.status === "ready" ? <Editor initial={state.data} onReload={reload} /> : null}
    </section>
  );
}
