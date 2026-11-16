import type { Draft } from "../api/types";

// Read-only view of the structured draft. Quantities are decimal TEXT and are printed exactly as received.

function Text({ value }: { value: string | null }) {
  return value === null || value === "" ? <span className="muted">—</span> : <>{value}</>;
}

export function DraftView({ draft }: { draft: Draft }) {
  return (
    <div className="draft">
      <dl className="facts">
        <div>
          <dt>Requester</dt>
          <dd>
            <Text value={draft.requester.name} />
          </dd>
        </div>
        <div>
          <dt>Contact</dt>
          <dd>
            <Text value={draft.requester.contact} />
          </dd>
        </div>
        <div>
          <dt>Notes</dt>
          <dd className="preserve">
            <Text value={draft.notes} />
          </dd>
        </div>
      </dl>

      <h3>Items ({draft.items.length})</h3>
      {draft.items.length === 0 ? (
        <p className="muted">No items.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">#</th>
                <th scope="col">Description</th>
                <th scope="col" className="num">
                  Quantity
                </th>
                <th scope="col">Unit</th>
                <th scope="col">Notes</th>
              </tr>
            </thead>
            <tbody>
              {draft.items.map((item, index) => (
                <tr key={index}>
                  <td>{index + 1}</td>
                  <td>
                    <Text value={item.description} />
                  </td>
                  <td className="num">
                    <Text value={item.quantity} />
                  </td>
                  <td>
                    <Text value={item.unit} />
                  </td>
                  <td>
                    <Text value={item.notes} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {draft.source_refs.length > 0 ? (
        <details className="source-refs">
          <summary>Source references ({draft.source_refs.length})</summary>
          <ul>
            {draft.source_refs.map((ref, index) => (
              <li key={index}>
                <code>{ref.field_path}</code>
                {ref.line !== null ? <span className="muted"> line {ref.line}</span> : null}
                <blockquote className="excerpt">{ref.excerpt}</blockquote>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}
