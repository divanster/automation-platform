import { useId, type ReactNode } from "react";

import type { AiCatalog, AiProfileRef, AiProviderType } from "../../api/types";
import { MAX_PROFILES, describeCodes, localityLabel, typeOf, withProviderType, type ConnectionDraft } from "../../lib/aiSettingsForm";
import { Icon } from "../ui/Icon";

/** The relative problems of one connection: `{"endpoint": [codes], "profiles[0].model_id": [codes]}`. */
export type ConnectionProblems = Record<string, string[]>;

interface ControlProps {
  id: string;
  "aria-invalid"?: true;
  "aria-describedby"?: string;
}

function Field({ label, hint, codes, children }: { label: string; hint?: ReactNode; codes?: string[]; children: (props: ControlProps) => ReactNode }) {
  const id = useId();
  const message = describeCodes(codes);
  const describedBy = [hint ? `${id}-hint` : null, message ? `${id}-message` : null].filter(Boolean).join(" ");
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children({ id, "aria-invalid": message ? true : undefined, "aria-describedby": describedBy || undefined })}
      {hint ? (
        <p id={`${id}-hint`} className="field-hint">
          {hint}
        </p>
      ) : null}
      {message ? (
        <p id={`${id}-message`} className="field-error">
          {message}
        </p>
      ) : null}
    </div>
  );
}

function credentialHint(type: AiProviderType, locality: string, prefix: string): ReactNode {
  const need =
    type.credential === "required_unless_local" ? (locality === "local" ? "Optional for a local endpoint." : "Required for this locality.") : "Optional.";
  return (
    <>
      {need} The <strong>name</strong> of a backend environment variable, starting with <code>{prefix}</code>. The key itself is set by the operator in
      the backend environment; it is never typed, sent or shown here.
    </>
  );
}

const MODEL_NOTE: Record<string, string> = {
  ollama: "The model must already be installed in Ollama. This application never downloads, pulls, lists or deletes models.",
  openai_compatible: "Name the model identifiers your server offers. This application does not list or download them.",
};

/**
 * The form of ONE connection. It edits a draft; nothing is sent until the page's Save. It has no field for a key, token,
 * password or header: the credential reference is a variable NAME, and a value that is not shaped like one is refused
 * before anything is sent.
 */
export function ConnectionEditor({
  draft,
  catalog,
  problems,
  onChange,
  onDone,
}: {
  draft: ConnectionDraft;
  catalog: AiCatalog;
  problems: ConnectionProblems;
  onChange(next: ConnectionDraft): void;
  onDone(): void;
}) {
  const type = typeOf(catalog, draft.provider_type);
  const set = (patch: Partial<ConnectionDraft>) => onChange({ ...draft, ...patch });

  function setProfile(index: number, patch: Partial<AiProfileRef>) {
    set({ profiles: draft.profiles.map((p, i) => (i === index ? { ...p, ...patch } : p)) });
  }

  function toggleFixedProfile(profile: AiProfileRef, on: boolean) {
    set({
      profiles: on
        ? [...draft.profiles, { profile_id: profile.profile_id, model_id: profile.model_id }]
        : draft.profiles.filter((p) => p.profile_id !== profile.profile_id),
    });
  }

  return (
    <div
      className="connection-editor"
      role="group"
      aria-label={`Edit connection ${draft.display_name || "(new)"}`}
      onKeyDown={(event) => {
        // Enter in a field must not save the whole page by accident.
        if (event.key === "Enter" && event.target instanceof HTMLInputElement && event.target.type !== "checkbox") event.preventDefault();
      }}
    >
      <div className="field-row">
        <Field label="Display name" codes={problems.display_name}>
          {(props) => <input {...props} type="text" autoComplete="off" value={draft.display_name} maxLength={120} onChange={(e) => set({ display_name: e.target.value })} />}
        </Field>
        <Field
          label="Identifier"
          hint={draft.isNew ? "Lower-case letters, digits, hyphen or underscore. It cannot be changed after the first save." : "Fixed after the first save."}
          codes={problems.connection_id}
        >
          {(props) => (
            <input
              {...props}
              type="text"
              autoComplete="off"
              spellCheck={false}
              value={draft.connection_id}
              readOnly={!draft.isNew}
              maxLength={64}
              onChange={(e) => set({ connection_id: e.target.value })}
            />
          )}
        </Field>
      </div>

      <div className="field-row">
        <Field label="Provider type" hint={draft.isNew ? undefined : "Fixed after the first save. Remove and add a connection to change it."} codes={problems.provider_type}>
          {(props) => (
            <select
              {...props}
              value={draft.provider_type}
              disabled={!draft.isNew}
              onChange={(e) => {
                const next = typeOf(catalog, e.target.value);
                if (next) onChange(withProviderType(draft, next));
              }}
            >
              {catalog.provider_types.map((t) => (
                <option key={t.provider_type} value={t.provider_type}>
                  {t.label}
                </option>
              ))}
            </select>
          )}
        </Field>
        <Field label="Data locality" hint="Where the model runs. The data policy chosen on this page is checked against it." codes={problems.locality}>
          {(props) => (
            <select {...props} value={draft.locality} disabled={!type || type.localities.length < 2} onChange={(e) => set({ locality: e.target.value })}>
              {(type?.localities ?? [draft.locality]).map((locality) => (
                <option key={locality} value={locality}>
                  {localityLabel(locality)}
                </option>
              ))}
            </select>
          )}
        </Field>
      </div>

      {type?.needs_endpoint ? (
        <Field
          label="Endpoint"
          hint={
            <>
              The server's address, for example <code>https://host/v1</code>. The operator must also approve its origin in the backend environment; a saved
              endpoint cannot widen that list.
            </>
          }
          codes={problems.endpoint}
        >
          {(props) => (
            <input {...props} type="text" inputMode="url" autoComplete="off" autoCapitalize="off" spellCheck={false} value={draft.endpoint} maxLength={600} onChange={(e) => set({ endpoint: e.target.value })} />
          )}
        </Field>
      ) : null}

      {type && type.credential !== "none" ? (
        <Field label="Credential reference (variable name)" hint={credentialHint(type, draft.locality, catalog.credential_ref_prefix)} codes={problems.credential_ref}>
          {(props) => (
            <input
              {...props}
              type="text"
              autoComplete="off"
              autoCapitalize="off"
              spellCheck={false}
              value={draft.credential_ref}
              maxLength={80}
              onChange={(e) => set({ credential_ref: e.target.value })}
            />
          )}
        </Field>
      ) : null}

      <fieldset className="profile-set">
        <legend>Model profiles</legend>
        <p className="field-hint">{(type && MODEL_NOTE[type.provider_type]) ?? "The models this connection may use. Nothing is downloaded automatically."}</p>
        {type?.profile_source === "fixed" ? (
          type.profiles.length === 0 ? (
            <p className="muted">This provider type offers no profiles.</p>
          ) : (
            type.profiles.map((profile) => (
              <label key={profile.profile_id} className="ack-row">
                <input type="checkbox" checked={draft.profiles.some((p) => p.profile_id === profile.profile_id)} onChange={(e) => toggleFixedProfile(profile, e.target.checked)} />
                <span>
                  {profile.profile_id} <span className="muted">— model {profile.model_id}</span>
                </span>
              </label>
            ))
          )
        ) : (
          <>
            {draft.profiles.map((profile, index) => (
              <div key={index} className="profile-row">
                <Field label={`Profile ${index + 1} identifier`} codes={problems[`profiles[${index}].profile_id`]}>
                  {(props) => <input {...props} type="text" autoComplete="off" spellCheck={false} value={profile.profile_id} maxLength={80} onChange={(e) => setProfile(index, { profile_id: e.target.value })} />}
                </Field>
                <Field label={`Profile ${index + 1} model`} codes={problems[`profiles[${index}].model_id`]}>
                  {(props) => <input {...props} type="text" autoComplete="off" spellCheck={false} value={profile.model_id} maxLength={140} onChange={(e) => setProfile(index, { model_id: e.target.value })} />}
                </Field>
                <button
                  type="button"
                  className="button button-small"
                  aria-label={`Remove profile ${index + 1}`}
                  onClick={() => set({ profiles: draft.profiles.filter((_, i) => i !== index) })}
                >
                  <Icon name="trash" />
                  Remove
                </button>
              </div>
            ))}
            <button type="button" className="button button-small" disabled={draft.profiles.length >= MAX_PROFILES} onClick={() => set({ profiles: [...draft.profiles, { profile_id: "", model_id: "" }] })}>
              <Icon name="plus" />
              Add model profile
            </button>
          </>
        )}
        {problems.profiles ? <p className="field-error">{describeCodes(problems.profiles)}</p> : null}
      </fieldset>

      <label className="ack-row">
        <input type="checkbox" checked={draft.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
        <span>
          Enabled <span className="muted">— a disabled connection cannot be selected for extraction.</span>
        </span>
      </label>

      <div className="form-actions">
        <button type="button" className="button" onClick={onDone}>
          Done editing
        </button>
      </div>
    </div>
  );
}
