import { useId, useRef, useState, type FormEvent } from "react";

import { getRequest } from "../api/requests";
import { editRequest } from "../api/review";
import type { Draft, EditResponse, RequestDetail } from "../api/types";
import {
  addItem,
  displayedPaths,
  fieldErrorPathToFindingPath,
  formFromDraft,
  groupByPath,
  isDirty,
  itemPath,
  payloadFromForm,
  removeItem,
  setItemField,
  setTopField,
  TOP_FIELD_PATHS,
  type DraftForm,
  type FormField,
  type PathFinding,
} from "../lib/draftForm";
import { describeError, describeFieldCode, fieldCodesFrom, findingsFrom, isOutOfDate } from "../lib/errors";
import { ApiError } from "../api/http";
import { ConfirmStep } from "./ConfirmStep";
import { DraftView } from "./DraftView";
import { Notice } from "./Status";

// A structured form for the draft of a request. It starts from the draft the server sent for the current revision (or for
// the extraction result when no revision exists yet), keeps untouched values exactly as received, and saves only when the
// user presses the button. The server validates and decides everything; findings it reports appear beside their fields.
//
// BASE REVISION. The form remembers the revision it was loaded from and sends it as `base_revision_id`. If someone else
// saved in the meantime the server answers 409 `stale_base_revision`; the form then keeps the user's text, loads the latest
// version, and offers two explicit choices instead of overwriting either side silently.

interface Props {
  detail: RequestDetail;
  /** The draft to start from (the page passes `detail.draft`; the editor is not offered when there is none). */
  draft: Draft;
  onSaved: (result: EditResponse) => void;
  /** The user chose to leave the editor (nothing saved). `reload` asks the page to load the latest data first. */
  onClose: (options?: { reload?: boolean }) => void;
  /** The server said the page is out of date for a reason other than the revision (state changed, and so on). */
  onOutOfDate: (error: ApiError) => void;
}

interface Conflict {
  latest: RequestDetail;
}

function FieldFindings({ id, findings }: { id: string; findings: PathFinding[] | undefined }) {
  if (!findings || findings.length === 0) return null;
  return (
    <ul id={id} className="field-findings">
      {findings.map((f, i) => (
        <li key={`${f.code}:${i}`} className={f.blocking ? "field-finding blocking" : "field-finding warning"}>
          <span className="finding-kind">{f.blocking ? "Blocking" : "Warning"}</span> <code>{f.code}</code> {f.message}
        </li>
      ))}
    </ul>
  );
}

function TextField({
  label,
  field,
  onChange,
  findings,
  multiline = false,
  inputMode,
}: {
  label: string;
  field: FormField;
  onChange: (value: string) => void;
  findings: PathFinding[] | undefined;
  multiline?: boolean;
  inputMode?: "decimal";
}) {
  const findingsId = useId();
  const invalid = Boolean(findings?.some((f) => f.blocking));
  const common = {
    value: field.value,
    "aria-invalid": invalid || undefined,
    "aria-describedby": findings && findings.length > 0 ? findingsId : undefined,
  };
  return (
    <div className="field">
      <label>
        {label}
        {multiline ? (
          <textarea rows={2} {...common} onChange={(e) => onChange(e.target.value)} />
        ) : (
          <input type="text" inputMode={inputMode} autoComplete="off" spellCheck={false} {...common} onChange={(e) => onChange(e.target.value)} />
        )}
      </label>
      <FieldFindings id={findingsId} findings={findings} />
    </div>
  );
}

export function DraftEditor({ detail, draft, onSaved, onClose, onOutOfDate }: Props) {
  const runId = detail.run_id;
  // The revision this form started from. It changes only when the user explicitly chooses to continue on top of a newer one.
  const baseRevisionId = useRef<string | null>(detail.current_revision?.id ?? null);
  const baseNumber = useRef<number | null>(detail.current_revision?.number ?? null);

  const [form, setForm] = useState<DraftForm>(() => formFromDraft(draft));
  const [findings, setFindings] = useState<PathFinding[]>(detail.findings);
  const [findingsOrigin, setFindingsOrigin] = useState<"loaded" | "save" | "cleared">("loaded");
  const [message, setMessage] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [conflict, setConflict] = useState<Conflict | null>(null);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const submitting = useRef(false);

  const dirty = isDirty(form);
  const grouped = groupByPath(findings);
  const shown = displayedPaths(form);
  const unplaced = findings.filter((f) => !shown.has(f.field_path));

  function change(update: (current: DraftForm) => DraftForm) {
    setForm(update);
    setMessage(null);
  }

  /** Findings name items by position; after an item is added or removed the positions no longer match, so they are cleared. */
  function structureChanged(next: DraftForm) {
    setForm(next);
    setMessage(null);
    if (findings.length > 0) {
      setFindings([]);
      setFindingsOrigin("cleared");
    }
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current || saving || conflict || !dirty) return;
    submitting.current = true;
    setSaving(true);
    setMessage(null);
    try {
      const result = await editRequest(runId, baseRevisionId.current, payloadFromForm(form));
      onSaved(result);
    } catch (error) {
      await handleError(error);
    } finally {
      submitting.current = false;
      setSaving(false);
    }
  }

  async function handleError(error: unknown) {
    const blocking = findingsFrom(error);
    if (blocking) {
      setFindings(blocking);
      setFindingsOrigin("save");
      setMessage(describeError(error));
      return;
    }
    if (error instanceof ApiError && error.code === "validation_error") {
      const converted: PathFinding[] = Object.entries(fieldCodesFrom(error)).flatMap(([path, codes]) =>
        codes.map((code) => ({ code, field_path: fieldErrorPathToFindingPath(path), blocking: true, message: describeFieldCode(code) })),
      );
      setFindings(converted);
      setFindingsOrigin("save");
      setMessage(describeError(error));
      return;
    }
    if (error instanceof ApiError && error.code === "draft_not_serializable") {
      const path = typeof error.details?.path === "string" ? error.details.path : "";
      setFindings([{ code: error.code, field_path: path, blocking: true, message: describeError(error) }]);
      setFindingsOrigin("save");
      setMessage(describeError(error));
      return;
    }
    if (error instanceof ApiError && error.code === "stale_base_revision") {
      // Never lose what the user typed: fetch the latest version and let them decide.
      try {
        const latest = await getRequest(runId);
        setConflict({ latest });
        setMessage(describeError(error));
      } catch (loadError) {
        setMessage(`${describeError(error)} The latest version could not be loaded: ${describeError(loadError)}`);
      }
      return;
    }
    if (error instanceof ApiError && isOutOfDate(error)) {
      setMessage(describeError(error));
      onOutOfDate(error);
      return;
    }
    setMessage(describeError(error));
  }

  function keepMineOnTopOfLatest() {
    if (!conflict) return;
    baseRevisionId.current = conflict.latest.current_revision?.id ?? null;
    baseNumber.current = conflict.latest.current_revision?.number ?? null;
    setConflict(null);
    setMessage(
      "Your version is now based on the latest revision. Saving it will REPLACE the content of that revision with what is in this form. Review it, then save.",
    );
  }

  function requestClose() {
    if (dirty && !confirmDiscard) setConfirmDiscard(true);
    else onClose();
  }

  const busy = saving;
  const latestDraft = conflict?.latest.draft ?? null;

  return (
    <form className="editor" onSubmit={onSubmit} aria-labelledby="editor-title" noValidate>
      <h3 id="editor-title">
        Edit draft
        {baseNumber.current !== null ? <span className="muted"> · based on revision {baseNumber.current}</span> : <span className="muted"> · first edit (no revision yet)</span>}
      </h3>
      <p className="muted">
        Saving creates a new revision; the earlier ones are kept. Values you leave unchanged are sent exactly as they are. Nothing is saved until you press
        “Save as new revision”.
      </p>

      {message ? <Notice kind={conflict || findingsOrigin === "save" ? "error" : "info"}>{message}</Notice> : null}

      {conflict ? (
        <div className="conflict" role="alert">
          <p>
            <strong>This request was changed while you were editing.</strong> It is now at revision {conflict.latest.current_revision?.number ?? "—"}
            {conflict.latest.state !== detail.state ? ` and its state is ${conflict.latest.state}` : ""}. Your changes are still in the form below and have not been saved.
          </p>
          {latestDraft ? (
            <details>
              <summary>Show the latest version</summary>
              <DraftView draft={latestDraft} />
            </details>
          ) : null}
          <div className="form-actions">
            <button type="button" className="button" onClick={() => onClose({ reload: true })}>
              Discard my changes and load the latest
            </button>
            <button type="button" className="button button-primary" onClick={keepMineOnTopOfLatest}>
              Keep my version and base it on the latest
            </button>
          </div>
        </div>
      ) : null}

      {findingsOrigin === "save" ? <p className="muted">Findings below come from your last save attempt.</p> : null}
      {findingsOrigin === "cleared" ? <p className="muted">Items were added or removed, so earlier findings were cleared. Save to see the current ones.</p> : null}
      {unplaced.length > 0 ? (
        <section aria-label="Other findings" className="card-inner">
          <h4>Other findings</h4>
          <FieldFindings id="unplaced-findings" findings={unplaced} />
        </section>
      ) : null}

      <fieldset disabled={busy || conflict !== null}>
        <legend>Requester</legend>
        <TextField
          label="Name"
          field={form.requesterName}
          onChange={(v) => change((f) => setTopField(f, "requesterName", v))}
          findings={grouped.get(TOP_FIELD_PATHS.requesterName)}
        />
        <TextField
          label="Contact"
          field={form.requesterContact}
          onChange={(v) => change((f) => setTopField(f, "requesterContact", v))}
          findings={grouped.get(TOP_FIELD_PATHS.requesterContact)}
        />
      </fieldset>

      <fieldset disabled={busy || conflict !== null}>
        <legend>Items ({form.items.length})</legend>
        <FieldFindings id="items-findings" findings={grouped.get("items")} />
        {form.items.length === 0 ? <p className="muted">No items.</p> : null}
        {form.items.map((item, index) => (
          <div key={item.key} className="item-card" role="group" aria-label={`Item ${index + 1}`}>
            <div className="item-head">
              <strong>Item {index + 1}</strong>
              <button
                type="button"
                className="button button-small"
                aria-label={`Remove item ${index + 1}`}
                onClick={() => structureChanged(removeItem(form, index))}
              >
                Remove
              </button>
            </div>
            <FieldFindings id={`item-${item.key}-findings`} findings={grouped.get(itemPath(index))} />
            <TextField
              label={`Description (item ${index + 1})`}
              field={item.description}
              onChange={(v) => change((f) => setItemField(f, item.key, "description", v))}
              findings={grouped.get(itemPath(index, "description"))}
              multiline
            />
            <div className="item-row">
              <TextField
                label={`Quantity (item ${index + 1})`}
                field={item.quantity}
                onChange={(v) => change((f) => setItemField(f, item.key, "quantity", v))}
                findings={grouped.get(itemPath(index, "quantity"))}
                inputMode="decimal"
              />
              <TextField
                label={`Unit (item ${index + 1})`}
                field={item.unit}
                onChange={(v) => change((f) => setItemField(f, item.key, "unit", v))}
                findings={grouped.get(itemPath(index, "unit"))}
              />
            </div>
            <TextField
              label={`Notes (item ${index + 1})`}
              field={item.notes}
              onChange={(v) => change((f) => setItemField(f, item.key, "notes", v))}
              findings={grouped.get(itemPath(index, "notes"))}
              multiline
            />
          </div>
        ))}
        <button type="button" className="button" onClick={() => structureChanged(addItem(form))}>
          Add item
        </button>
        {form.droppedRefs > 0 ? (
          <p className="muted" role="status">
            {form.droppedRefs} source reference{form.droppedRefs === 1 ? "" : "s"} to removed items {form.droppedRefs === 1 ? "was" : "were"} dropped, and the references of later
            items were renumbered.
          </p>
        ) : null}
      </fieldset>

      <fieldset disabled={busy || conflict !== null}>
        <legend>Request notes</legend>
        <TextField
          label="Notes"
          field={form.notes}
          onChange={(v) => change((f) => setTopField(f, "notes", v))}
          findings={grouped.get(TOP_FIELD_PATHS.notes)}
          multiline
        />
      </fieldset>

      <p className="muted">
        {form.sourceRefs.length} source reference{form.sourceRefs.length === 1 ? "" : "s"} are kept with the draft and are not editable here.
      </p>

      {confirmDiscard ? (
        <ConfirmStep
          title="Discard your changes?"
          confirmLabel="Discard changes"
          busy={false}
          danger
          onConfirm={() => onClose()}
          onCancel={() => setConfirmDiscard(false)}
        >
          <p>Nothing has been saved. Your edits will be lost.</p>
        </ConfirmStep>
      ) : (
        <div className="form-actions">
          <button type="submit" className="button button-primary" disabled={!dirty || busy || conflict !== null}>
            {saving ? "Saving…" : "Save as new revision"}
          </button>
          <button type="button" className="button" onClick={requestClose} disabled={busy}>
            Cancel
          </button>
          {!dirty ? <span className="muted">No changes yet.</span> : null}
        </div>
      )}
    </form>
  );
}
