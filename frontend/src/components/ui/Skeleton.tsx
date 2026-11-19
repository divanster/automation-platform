/** Grey placeholder lines shown while content loads. Purely visual: the surrounding status element carries the announcement. */
export function Skeleton({ width, className = "" }: { width?: string; className?: string }) {
  return <span className={`skeleton ${className}`.trim()} style={width ? { width } : undefined} aria-hidden="true" />;
}

export function SkeletonRows({ rows = 4 }: { rows?: number }) {
  return (
    <div className="skeleton-rows" aria-hidden="true">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} width={i % 3 === 2 ? "70%" : "100%"} />
      ))}
    </div>
  );
}
