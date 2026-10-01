/**
 * Dates, written the way a person would say them.
 *
 * The subtlety this exists for: a date-only string is a *day*, not an instant.
 * `new Date("2024-04-06")` parses it as midnight UTC, which in every timezone
 * west of London is the evening of the 5th — so a deadline of 6 April rendered
 * as "5 Apr 2024". Splitting the parts and building a local date has no such
 * problem, and a timestamp with a zone in it is left alone because that one
 * really is an instant.
 */
export function shortDate(iso: string | null | undefined, opts?: { year?: boolean }): string {
  const when = asDate(iso);
  if (!when) return iso ? String(iso).slice(0, 10) : "";
  const sameYear = when.getFullYear() === new Date().getFullYear();
  return when.toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    ...(opts?.year === false && sameYear ? {} : { year: "numeric" }),
  });
}

/** A Date, or null for anything that is not one. Day-only strings stay local. */
export function asDate(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const dayOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  const when = dayOnly
    ? new Date(Number(dayOnly[1]), Number(dayOnly[2]) - 1, Number(dayOnly[3]))
    : new Date(iso);
  return Number.isNaN(when.getTime()) ? null : when;
}

/** Whether a day has already passed. Compared by day, not by instant. */
export function isPast(iso: string | null | undefined): boolean {
  const when = asDate(iso);
  if (!when) return false;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return when < today;
}
