import type { EmptyReason } from "../api";

/**
 * Why there is nothing, never just nothing.
 *
 * "No confident matches" and "nothing matched" are different facts and lead to
 * different next actions: one has a retry that will show the weak results, the
 * other means the archive genuinely does not contain this.
 */
export function EmptyState({
  reason,
  onRetry,
}: {
  reason: EmptyReason;
  onRetry: (params: Record<string, string | number>) => void;
}) {
  const headline =
    reason.code === "below_confidence_floor"
      ? "No confident matches"
      : reason.code === "no_matches"
        ? "Nothing matched"
        : reason.code === "no_vectors_for_model"
          ? "Nothing embedded yet"
          : "Type to search";

  return (
    <div className="empty">
      <div className="empty-headline">{headline}</div>
      <div className="empty-detail">{reason.message}</div>

      {reason.code === "below_confidence_floor" && reason.best_score !== null && (
        <div className="empty-detail dim">
          best {reason.best_score} against a floor of {reason.floor}
        </div>
      )}
      {reason.code === "no_vectors_for_model" && (
        <div className="empty-detail dim">Run <code>muster embed</code>.</div>
      )}
      {reason.retry && (
        <button className="retry" onClick={() => onRetry(reason.retry!)}>
          Show weak matches anyway
        </button>
      )}
    </div>
  );
}
