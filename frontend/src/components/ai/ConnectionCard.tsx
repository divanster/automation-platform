import { useId } from "react";

import type { AiCatalog, AiConnection } from "../../api/types";
import {
  connectionFields,
  describeStatus,
  localityLabel,
  providerTypeLabel,
  type ConnectionDraft,
} from "../../lib/aiSettingsForm";
import { ConfirmStep } from "../ConfirmStep";
import { Icon } from "../ui/Icon";
import { ConnectionEditor, type ConnectionProblems } from "./ConnectionEditor";

export type CardConfirm = "disable" | "remove" | null;

/** One named connection: what it is, whether this process can use it, and the actions on it. Edits stay in the draft until the page saves. */
export function ConnectionCard({
  draft,
  catalog,
  saved,
  problems,
  active,
  open,
  confirm,
  onOpen,
  onClose,
  onChange,
  onAskDisable,
  onAskRemove,
  onEnable,
  onConfirm,
  onCancelConfirm,
}: {
  draft: ConnectionDraft;
  catalog: AiCatalog;
  /** The stored version (with the status of that version), or undefined for a connection that has not been saved. */
  saved: AiConnection | undefined;
  problems: ConnectionProblems;
  /** It is the connection the worker would use after a restart, as currently drafted. */
  active: boolean;
  open: boolean;
  confirm: CardConfirm;
  onOpen(): void;
  onClose(): void;
  onChange(next: ConnectionDraft): void;
  onAskDisable(): void;
  onAskRemove(): void;
  onEnable(): void;
  onConfirm(): void;
  onCancelConfirm(): void;
}) {
  const titleId = useId();
  const fields = connectionFields(draft, catalog);
  const name = draft.display_name.trim() || "New connection";
  const edited = saved !== undefined && JSON.stringify(fields) !== JSON.stringify(connectionFields({ ...draft, ...savedAsDraft(saved) }, catalog));
  const status = saved ? describeStatus(saved.status) : null;
  const problemCount = Object.keys(problems).length;

  return (
    <li className="connection-card" data-active={active || undefined}>
      <article aria-labelledby={titleId}>
        <div className="connection-head">
          <h3 id={titleId}>{name}</h3>
          <div className="badge-row">
            {active ? <span className="badge badge-ok">Active</span> : null}
            <span className="badge">{providerTypeLabel(catalog, draft.provider_type)}</span>
            <span className="badge">{localityLabel(draft.locality)}</span>
            <span className={draft.enabled ? "badge badge-ok" : "badge badge-muted"}>{draft.enabled ? "Enabled" : "Disabled"}</span>
            {status ? (
              <span className={status.ok ? "badge badge-ok" : "badge badge-warn"}>{status.label}</span>
            ) : (
              <span className="badge badge-muted">Not saved yet</span>
            )}
            {edited ? <span className="badge badge-info">Edited</span> : null}
            {problemCount > 0 ? <span className="badge badge-danger">Needs attention</span> : null}
          </div>
        </div>

        <dl className="connection-facts">
          <div>
            <dt>Identifier</dt>
            <dd>{fields.connection_id || "—"}</dd>
          </div>
          {fields.endpoint ? (
            <div>
              <dt>Endpoint</dt>
              <dd>{fields.endpoint}</dd>
            </div>
          ) : null}
          <div>
            <dt>Credential reference</dt>
            <dd>{fields.credential_ref ?? "None"}</dd>
          </div>
          <div>
            <dt>Models</dt>
            <dd>
              {fields.profiles.length === 0
                ? "None"
                : fields.profiles.map((p) => (
                    <span key={`${p.profile_id}:${p.model_id}`} className="model-chip">
                      {p.profile_id} <span className="muted">→ {p.model_id}</span>
                    </span>
                  ))}
            </dd>
          </div>
        </dl>

        {status && status.detail ? <p className="status-detail">{status.detail}</p> : null}
        {saved && edited ? <p className="muted">The status shown is that of the saved version.</p> : null}

        {open ? (
          <ConnectionEditor draft={draft} catalog={catalog} problems={problems} onChange={onChange} onDone={onClose} />
        ) : problemCount > 0 ? (
          <p className="field-error">This connection has problems. Open it to see them.</p>
        ) : null}

        {confirm === "remove" ? (
          <ConfirmStep title={`Remove “${name}”?`} confirmLabel="Remove connection" danger busy={false} onConfirm={onConfirm} onCancel={onCancelConfirm}>
            <p>
              It leaves the list when you save the settings. Nothing is deleted from the backend environment, and the audit history keeps the record of it.
            </p>
          </ConfirmStep>
        ) : null}
        {confirm === "disable" ? (
          <ConfirmStep title={`Disable “${name}”?`} confirmLabel="Disable connection" danger busy={false} onConfirm={onConfirm} onCancel={onCancelConfirm}>
            <p>A disabled connection cannot be selected for extraction. This takes effect when you save the settings and restart the worker.</p>
          </ConfirmStep>
        ) : null}

        {confirm === null ? (
          <div className="connection-actions">
            {!open ? (
              <button type="button" className="button button-small" aria-label={`Edit ${name}`} onClick={onOpen}>
                <Icon name="edit" />
                Edit
              </button>
            ) : null}
            {draft.enabled ? (
              <button type="button" className="button button-small" aria-label={`Disable ${name}`} disabled={active} onClick={onAskDisable}>
                Disable
              </button>
            ) : (
              <button type="button" className="button button-small" aria-label={`Enable ${name}`} onClick={onEnable}>
                Enable
              </button>
            )}
            <button type="button" className="button button-small button-danger" aria-label={`Remove ${name}`} disabled={active} onClick={onAskRemove}>
              <Icon name="trash" />
              Remove
            </button>
            {active ? <span className="muted">This is the active connection. Choose another one, or deterministic mode, before disabling or removing it.</span> : null}
          </div>
        ) : null}
      </article>
    </li>
  );
}

function savedAsDraft(saved: AiConnection): Partial<ConnectionDraft> {
  return {
    connection_id: saved.connection_id,
    display_name: saved.display_name,
    provider_type: saved.provider_type,
    locality: saved.locality,
    endpoint: saved.endpoint ?? "",
    credential_ref: saved.credential_ref ?? "",
    enabled: saved.enabled,
    profiles: saved.profiles,
  };
}
