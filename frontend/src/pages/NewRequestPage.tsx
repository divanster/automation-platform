import { useRef, useState, type ChangeEvent, type FormEvent } from "react";
import { Link, useNavigate } from "react-router";

import { submitRequest } from "../api/requests";
import { useAuth } from "../auth/AuthContext";
import { Notice } from "../components/Status";
import { describeError } from "../lib/errors";
import { newClientRequestId } from "../lib/format";

const TEMPLATE = `Requester: <name>
Contact: <contact>                          (optional)
Item: <quantity> | <unit> | <description> | <notes>   (notes optional; one line per item)
Notes: <request-level notes>                (optional)`;

export function NewRequestPage() {
  const { can } = useAuth();
  const navigate = useNavigate();
  const [text, setText] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // One id per distinct text: resubmitting the SAME text after a lost response returns the original request instead of
  // creating a duplicate, while editing the text starts a new submission (the server refuses an id reused for other text).
  const clientRequestId = useRef(newClientRequestId());

  if (!can("intake.submit_request")) {
    return (
      <section>
        <h1>New request</h1>
        <Notice kind="warning">Your account does not have permission to submit requests.</Notice>
        <p>
          <Link to="/requests">Back to requests</Link>
        </p>
      </section>
    );
  }

  function onChange(event: ChangeEvent<HTMLTextAreaElement>) {
    setText(event.target.value);
    clientRequestId.current = newClientRequestId();
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      const created = await submitRequest(text, clientRequestId.current);
      navigate(`/requests/${created.run_id}`);
    } catch (caught) {
      setError(describeError(caught));
      setPending(false);
    }
  }

  return (
    <section>
      <h1>New request</h1>
      <p>
        Paste the request text. It is stored exactly as typed and queued for extraction by the background worker; the next page
        shows its progress.
      </p>
      <Notice kind="info">
        Extraction currently uses a deterministic <strong>test parser</strong>, not an AI model. It understands only this labelled-line
        format and ignores everything else:
        <pre className="template">{TEMPLATE}</pre>
      </Notice>
      <form onSubmit={onSubmit} className="card">
        {error ? <Notice kind="error">{error}</Notice> : null}
        <label>
          Request text
          <textarea
            name="text"
            rows={14}
            value={text}
            onChange={onChange}
            disabled={pending}
            spellCheck={false}
            aria-describedby="text-help"
          />
        </label>
        <p id="text-help" className="muted">
          The server checks the text and refuses anything it cannot store.
        </p>
        <div className="form-actions">
          <button type="submit" className="button button-primary" disabled={pending || text === ""}>
            {pending ? "Submitting…" : "Submit request"}
          </button>
          <Link className="button" to="/requests">
            Cancel
          </Link>
        </div>
      </form>
    </section>
  );
}
