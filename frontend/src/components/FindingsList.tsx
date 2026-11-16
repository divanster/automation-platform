import type { Finding } from "../api/types";

// Findings are review material and may quote the user's source text, so every field is rendered as plain text (React
// escapes it). The `code`, `field_path` and `blocking` flag are the stable contract; `message` is explanatory text for
// a person and is only displayed, never matched on.

function FindingRow({ finding }: { finding: Finding }) {
  return (
    <li className={finding.blocking ? "finding finding-blocking" : "finding finding-warning"}>
      <div className="finding-head">
        <span className="finding-kind">{finding.blocking ? "Blocking" : "Warning"}</span>
        <code>{finding.code}</code>
        <code className="muted">{finding.field_path || "(whole request)"}</code>
      </div>
      <p>{finding.message}</p>
      {finding.excerpt ? <blockquote className="excerpt">{finding.excerpt}</blockquote> : null}
    </li>
  );
}

export function FindingsList({ findings }: { findings: Finding[] }) {
  if (findings.length === 0) return <p className="muted">No findings.</p>;
  const blocking = findings.filter((f) => f.blocking);
  const warnings = findings.filter((f) => !f.blocking);
  return (
    <div className="findings">
      {blocking.length > 0 ? (
        <section aria-label="Blocking findings">
          <h3>Blocking findings ({blocking.length})</h3>
          <ul className="finding-list">
            {blocking.map((f, i) => (
              <FindingRow key={`${f.code}:${f.field_path}:${i}`} finding={f} />
            ))}
          </ul>
        </section>
      ) : null}
      {warnings.length > 0 ? (
        <section aria-label="Warnings">
          <h3>Warnings ({warnings.length})</h3>
          <ul className="finding-list">
            {warnings.map((f, i) => (
              <FindingRow key={`${f.code}:${f.field_path}:${i}`} finding={f} />
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
