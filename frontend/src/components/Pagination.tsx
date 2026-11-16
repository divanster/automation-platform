export function Pagination({
  page,
  pageSize,
  count,
  onPage,
  noun = "request",
}: {
  page: number;
  pageSize: number;
  count: number;
  onPage: (page: number) => void;
  /** Singular noun for the total ("request", "event"); an "s" is added for any count other than 1. */
  noun?: string;
}) {
  const pages = Math.max(1, Math.ceil(count / pageSize));
  return (
    <nav className="pagination" aria-label="Pagination">
      <button type="button" className="button" disabled={page <= 1} onClick={() => onPage(page - 1)}>
        Previous
      </button>
      <span aria-live="polite">
        Page {page} of {pages} ({count} {count === 1 ? noun : `${noun}s`})
      </span>
      <button type="button" className="button" disabled={page >= pages} onClick={() => onPage(page + 1)}>
        Next
      </button>
    </nav>
  );
}
