import { useCallback, useEffect, useId, useMemo, useRef, useState, type FormEvent } from "react";

import { createAiCredential, deleteAiCredential, getAiCredentialStatus, replaceAiCredential } from "../../api/aiCredentials";
import { ApiError } from "../../api/http";
import type { AiConnection, AiCredentialStatus } from "../../api/types";
import { fieldCodesFrom } from "../../lib/errors";
import {
  checkSecret,
  connectionsReading,
  describeCredentialError,
  describeCredentialField,
  describeCredentialStatus,
  managedReferences,
  referenceProblem,
  type CredentialAction,
} from "../../lib/aiCredentials";
import { ConfirmStep } from "../ConfirmStep";
import { Notice } from "../Status";
import { Icon } from "../ui/Icon";
import { useToast } from "../ui/Toast";

type RowState = { phase: "loading" } | { phase: "ready"; status: AiCredentialStatus } | { phase: "error"; error: ApiError };

type Open = { reference: string; action: CredentialAction } | null;

/** What a form needs to know about an attempt. Never carries the secret. */
interface Outcome {
  ok: boolean;
  message?: string;
  /** Codes per field of a 400 `validation_error` (`reference`, `value`). */
  fields?: Record<string, string[]>;
  /** The stored state was not what the screen showed (409 already exists, 404 not found): the entered secret must not be sent again by accident. */
  stale?: boolean;
}

const asApiError = (error: unknown) => (error instanceof ApiError ? error : new ApiError(0, "unexpected_response", ""));

/** The secret field: masked, never prefilled, with no way to reveal it. */
function SecretField({ id, value, disabled, problem, onChange }: { id: string; value: string; disabled: boolean; problem: string | null; onChange(next: string): void }) {
  return (
    <div className="field">
      <label htmlFor={id}>New secret value</label>
      <input
        id={id}
        type="password"
        autoComplete="new-password"
        autoCapitalize="off"
        spellCheck={false}
        value={value}
        disabled={disabled}
        aria-invalid={problem ? true : undefined}
        aria-describedby={`${id}-hint${problem ? ` ${id}-problem` : ""}`}
        onChange={(event) => onChange(event.target.value)}
      />
      <p id={`${id}-hint`} className="field-hint">
        Stored encrypted by the server and never shown again, not even to administrators. Paste it without a space or line break at either end.
      </p>
      {problem ? (
        <p id={`${id}-problem`} className="field-error">
          {problem}
        </p>
      ) : null}
    </div>
  );
}

/** Entry of a secret for one known reference: store it (create) or replace it (after a confirmation). */
function RowSecretForm({ reference, action, onSubmit, onCancel }: { reference: string; action: "create" | "replace"; onSubmit(value: string): Promise<Outcome>; onCancel(): void }) {
  const id = useId();
  const [value, setValue] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [working, setWorking] = useState(false);

  async function send() {
    setWorking(true);
    try {
      const outcome = await onSubmit(value);
      if (outcome.ok) setValue("");
      else if (outcome.fields?.value) {
        setProblem(describeCredentialField(outcome.fields.value));
        setConfirming(false);
      }
    } finally {
      setWorking(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (working) return;
    const code = checkSecret(value);
    if (code) {
      setProblem(describeCredentialField([code]));
      setConfirming(false);
      return;
    }
    setProblem(null);
    if (action === "replace" && !confirming) {
      setConfirming(true);
      return;
    }
    void send();
  }

  const verb = action === "create" ? "Store credential" : "Replace credential";
  return (
    <form className="credential-form" aria-label={`${verb} ${reference}`} onSubmit={submit} noValidate>
      <SecretField
        id={id}
        value={value}
        disabled={working}
        problem={problem}
        onChange={(next) => {
          setValue(next);
          setProblem(null);
          setConfirming(false);
        }}
      />
      {confirming ? (
        <ConfirmStep title={`Replace the stored credential ${reference}?`} confirmLabel="Replace credential" danger busy={working} onConfirm={() => void send()} onCancel={() => setConfirming(false)}>
          <p>The current value is overwritten and cannot be recovered. A running worker keeps using the value it read at start-up until it is restarted.</p>
        </ConfirmStep>
      ) : (
        <div className="form-actions">
          <button type="submit" className="button button-primary" disabled={working}>
            {working ? "Working…" : verb}
          </button>
          <button type="button" className="button" disabled={working} onClick={onCancel}>
            Cancel
          </button>
        </div>
      )}
    </form>
  );
}

function CredentialRow({
  reference,
  state,
  connections,
  open,
  note,
  onOpen,
  onClose,
  onRecheck,
  onSubmit,
  onDelete,
}: {
  reference: string;
  state: RowState;
  connections: AiConnection[];
  open: CredentialAction | null;
  note: string | null;
  onOpen(action: CredentialAction): void;
  onClose(): void;
  onRecheck(): void;
  onSubmit(action: "create" | "replace", value: string): Promise<Outcome>;
  onDelete(): Promise<Outcome>;
}) {
  const titleId = useId();
  const status = state.phase === "ready" ? state.status : null;
  const description = status ? describeCredentialStatus(status) : null;
  const readers = status ? status.connections : connectionsReading(connections, reference);
  const stored = status !== null && status.status !== "not_set";

  return (
    <li className="connection-card credential-row">
      <article aria-labelledby={titleId}>
        <div className="connection-head">
          <h3 id={titleId}>
            <code>{reference}</code>
          </h3>
          <div className="badge-row">
            {state.phase === "loading" ? <span className="badge badge-muted">Checking…</span> : null}
            {state.phase === "error" ? <span className="badge badge-warn">Status unknown</span> : null}
            {description ? <span className={`badge badge-${description.tone}`}>{description.label}</span> : null}
          </div>
        </div>
        <dl className="connection-facts">
          <div>
            <dt>Read by connections</dt>
            <dd>{readers.length > 0 ? readers.join(", ") : "None of the saved connections"}</dd>
          </div>
          {status ? (
            <div>
              <dt>Secret store</dt>
              <dd>{status.store}</dd>
            </div>
          ) : null}
        </dl>
        {description?.detail ? <p className="status-detail">{description.detail}</p> : null}
        {state.phase === "error" ? <p className="status-detail">{describeCredentialError(state.error, "status")}</p> : null}
        {status && readers.length === 0 ? <p className="muted">No saved connection reads this credential yet. Give a connection the source Managed and this reference, then save the settings.</p> : null}
        {note ? (
          <p className="field-error" role="alert">
            {note}
          </p>
        ) : null}

        {open === "create" || open === "replace" ? <RowSecretForm reference={reference} action={open} onSubmit={(value) => onSubmit(open, value)} onCancel={onClose} /> : null}
        {open === "delete" ? (
          <ConfirmStep title={`Delete the credential ${reference}?`} confirmLabel="Delete credential" danger busy={false} onConfirm={() => void onDelete()} onCancel={onClose}>
            <p>The stored value is removed from the server and cannot be recovered. A running worker keeps the value it read at start-up until it is restarted; after that restart a connection that still names it will not start.</p>
          </ConfirmStep>
        ) : null}

        {open === null ? (
          <div className="connection-actions">
            {status && status.status === "not_set" ? (
              <button type="button" className="button button-small" aria-label={`Store credential ${reference}`} onClick={() => onOpen("create")}>
                <Icon name="plus" />
                Store credential
              </button>
            ) : null}
            {stored ? (
              <button type="button" className="button button-small" aria-label={`Replace credential ${reference}`} onClick={() => onOpen("replace")}>
                <Icon name="edit" />
                Replace
              </button>
            ) : null}
            {stored ? (
              <button type="button" className="button button-small button-danger" aria-label={`Delete credential ${reference}`} onClick={() => onOpen("delete")}>
                <Icon name="trash" />
                Delete
              </button>
            ) : null}
            <button type="button" className="button button-small" aria-label={`Check status of ${reference}`} onClick={onRecheck}>
              Check status
            </button>
          </div>
        ) : null}
      </article>
    </li>
  );
}

function AddForm({
  prefix,
  onStore,
  onLookup,
}: {
  prefix: string;
  onStore(reference: string, value: string): Promise<Outcome>;
  onLookup(reference: string): void;
}) {
  const refId = useId();
  const secretId = useId();
  const [reference, setReference] = useState("");
  const [secret, setSecret] = useState("");
  const [problems, setProblems] = useState<{ reference: string | null; value: string | null; general: string | null }>({ reference: null, value: null, general: null });
  const [working, setWorking] = useState(false);

  function refProblem(): string | null {
    const code = referenceProblem(reference, prefix);
    return code ? describeCredentialField([code]) : null;
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (working) return;
    const secretCode = checkSecret(secret);
    const found = { reference: refProblem(), value: secretCode ? describeCredentialField([secretCode]) : null, general: null };
    setProblems(found);
    if (found.reference || found.value) return;
    setWorking(true);
    try {
      const outcome = await onStore(reference.trim(), secret);
      if (outcome.ok) {
        setSecret("");
        setReference("");
        setProblems({ reference: null, value: null, general: null });
      } else {
        setProblems({
          reference: describeCredentialField(outcome.fields?.reference),
          value: describeCredentialField(outcome.fields?.value),
          general: outcome.message ?? null,
        });
        if (outcome.stale) setSecret("");
      }
    } finally {
      setWorking(false);
    }
  }

  function lookup() {
    const problem = refProblem();
    setProblems({ reference: problem, value: null, general: null });
    if (!problem) {
      onLookup(reference.trim());
      setReference("");
    }
  }

  return (
    <form className="credential-form credential-add" aria-label="Store a new managed credential" onSubmit={(event) => void submit(event)} noValidate>
      <h3>Store a credential or look one up</h3>
      <div className="field">
        <label htmlFor={refId}>Credential reference</label>
        <input
          id={refId}
          type="text"
          autoComplete="off"
          autoCapitalize="off"
          spellCheck={false}
          value={reference}
          maxLength={80}
          disabled={working}
          aria-invalid={problems.reference ? true : undefined}
          onChange={(event) => setReference(event.target.value)}
        />
        <p className="field-hint">
          A name, not the key: <code>{prefix}</code> followed by capital letters, digits or underscores. A connection reads the credential through this name.
        </p>
        {problems.reference ? <p className="field-error">{problems.reference}</p> : null}
      </div>
      <SecretField id={secretId} value={secret} disabled={working} problem={problems.value} onChange={setSecret} />
      {problems.general ? (
        <p className="field-error" role="alert">
          {problems.general}
        </p>
      ) : null}
      <div className="form-actions">
        <button type="submit" className="button button-primary" disabled={working}>
          {working ? "Working…" : "Store new credential"}
        </button>
        <button type="button" className="button" disabled={working} onClick={lookup}>
          Check status only
        </button>
      </div>
    </form>
  );
}

/**
 * Management of the managed AI credentials (secrets in the backend's secret store). It sits OUTSIDE the settings form, so a secret can
 * never be part of an AI settings PUT: a secret lives only in the state of the one form that is open and goes only to /api/ai-credentials/.
 * It lists the references the SAVED connections read as managed credentials, plus any the administrator adds or looks up here; the API has
 * no list endpoint. The server decides everything; this panel only shows its answers.
 */
export function CredentialsPanel({ connections, prefix, onChanged }: { connections: AiConnection[]; prefix: string; onChanged(): void }) {
  const { notify } = useToast();
  const [extra, setExtra] = useState<string[]>([]);
  const [rows, setRows] = useState<Record<string, RowState>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [open, setOpen] = useState<Open>(null);
  const [changes, setChanges] = useState(0);
  const requested = useRef(new Set<string>());
  const latest = useRef<Record<string, number>>({});
  const inFlight = useRef(false);

  const saved = useMemo(() => managedReferences(connections), [connections]);
  const references = useMemo(() => [...saved, ...extra.filter((reference) => !saved.includes(reference))], [saved, extra]);
  const storeDisabled = Object.values(rows).some((row) => row.phase === "error" && row.error.code === "secret_store_disabled");

  const claim = (reference: string) => (latest.current[reference] = (latest.current[reference] ?? 0) + 1);

  const loadStatus = useCallback(async (reference: string) => {
    const token = claim(reference);
    setRows((current) => ({ ...current, [reference]: { phase: "loading" } }));
    try {
      const status = await getAiCredentialStatus(reference);
      if (latest.current[reference] === token) setRows((current) => ({ ...current, [reference]: { phase: "ready", status } }));
    } catch (error) {
      if (latest.current[reference] === token) setRows((current) => ({ ...current, [reference]: { phase: "error", error: asApiError(error) } }));
    }
  }, []);

  useEffect(() => {
    for (const reference of references) {
      if (!requested.current.has(reference)) {
        requested.current.add(reference);
        void loadStatus(reference);
      }
    }
  }, [references, loadStatus]);

  function lookUp(reference: string) {
    setExtra((current) => (current.includes(reference) || saved.includes(reference) ? current : [...current, reference]));
    requested.current.add(reference);
    void loadStatus(reference);
  }

  async function perform(reference: string, action: CredentialAction, value: string, scope: "row" | "add"): Promise<Outcome> {
    if (inFlight.current) return { ok: false, message: "Another credential operation is still running." };
    inFlight.current = true;
    try {
      const status =
        action === "create" ? await createAiCredential(reference, value) : action === "replace" ? await replaceAiCredential(reference, value) : await deleteAiCredential(reference);
      claim(reference);
      setRows((current) => ({ ...current, [reference]: { phase: "ready", status } }));
      setExtra((current) => (current.includes(reference) || saved.includes(reference) ? current : [...current, reference]));
      requested.current.add(reference);
      setNotes((current) => ({ ...current, [reference]: "" }));
      setOpen(null);
      setChanges((count) => count + 1);
      notify("success", action === "create" ? "Credential stored." : action === "replace" ? "Credential replaced." : "Credential deleted.");
      onChanged();
      return { ok: true };
    } catch (error) {
      const failure = asApiError(error);
      const message = describeCredentialError(failure, action);
      const stale = failure.code === "credential_already_exists" || failure.code === "credential_not_found";
      const fields = failure.code === "validation_error" ? fieldCodesFrom(failure) : undefined;
      if (stale) {
        // The stored state is not what the screen showed: show the current state and close the form, so nothing is overwritten or created by accident.
        lookUp(reference);
        setOpen(null);
      }
      if (scope === "row" && !fields) {
        setNotes((current) => ({ ...current, [reference]: message }));
        if (action === "delete") setOpen(null);
      }
      return { ok: false, message, fields, stale };
    } finally {
      inFlight.current = false;
    }
  }

  return (
    <section className="card credentials-panel" aria-labelledby="ai-credentials">
      <div className="section-head">
        <h2 id="ai-credentials">Managed credentials</h2>
      </div>
      <p className="muted">
        A key stored here is encrypted by the server&rsquo;s secret store and is never shown again. A connection reads it only when its credential source is <strong>Managed</strong> and its reference
        names it. Credentials set as backend environment variables are not managed here. Typing a key here never changes the AI settings; the settings above hold only the reference.
      </p>

      {changes > 0 ? (
        <Notice kind="warning">
          <strong>Worker restart required.</strong> The credential change is stored, but a running worker keeps the credential it read when it started. A new, replaced or deleted credential is not active until
          the worker is restarted. This page cannot restart it; ask the operator to stop and start the worker (see the runbook, section 5.11).
        </Notice>
      ) : null}
      {storeDisabled ? <Notice kind="error">The secret store is disabled on the server, so credentials cannot be stored, replaced or deleted. The operator must configure it (runbook section 5.11).</Notice> : null}

      {references.length === 0 ? (
        <p className="muted">No saved connection uses a managed credential. Store a credential below, then choose Managed and its reference on a connection.</p>
      ) : (
        <ul className="connection-list">
          {references.map((reference) => (
            <CredentialRow
              key={reference}
              reference={reference}
              state={rows[reference] ?? { phase: "loading" }}
              connections={connections}
              open={open && open.reference === reference ? open.action : null}
              note={notes[reference] || null}
              onOpen={(action) => {
                setNotes((current) => ({ ...current, [reference]: "" }));
                setOpen({ reference, action });
              }}
              onClose={() => setOpen(null)}
              onRecheck={() => {
                setNotes((current) => ({ ...current, [reference]: "" }));
                void loadStatus(reference);
              }}
              onSubmit={(action, value) => perform(reference, action, value, "row")}
              onDelete={() => perform(reference, "delete", "", "row")}
            />
          ))}
        </ul>
      )}

      <AddForm prefix={prefix} onStore={(reference, value) => perform(reference, "create", value, "add")} onLookup={lookUp} />
    </section>
  );
}
