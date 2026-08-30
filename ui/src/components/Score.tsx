import type { Score as ScoreT } from "../api";

/**
 * The score, and what it means.
 *
 * `of_best` rather than the raw value drives the bar: BM25 runs to ~45 and RRF
 * to ~0.033, so a bar scaled to the raw number would be meaningless in one mode
 * and invisible in the other.
 *
 * The arm badges are the part worth looking at. Two arms agreeing is far
 * stronger evidence than one, and a list of single-arm results with a flat
 * of_best is what "retrieval is guessing" looks like.
 */
export function Score({ score }: { score: ScoreT }) {
  const share = score.of_best ?? 0;
  const arms = score.arms ? Object.entries(score.arms) : [];
  const weak = arms.length === 1;

  return (
    <span className="score" title={`${score.value} (${score.scale})`}>
      {arms.length > 0 && (
        <span className={`arms${weak ? " arms-weak" : ""}`}>
          {arms.map(([name, rank]) => (
            <span key={name} className="arm" title={`${name} ranked this #${rank}`}>
              {name[0]!.toUpperCase()}
              {rank}
            </span>
          ))}
        </span>
      )}
      <span className="bar" aria-hidden="true">
        <span className="bar-fill" style={{ width: `${Math.round(share * 100)}%` }} />
      </span>
    </span>
  );
}
