/** Display helpers. Confidence is always a word, never a fake percentage. */

const MONTHS = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];

export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "unknown";
  return date.toISOString().replace("T", " ").slice(0, 16) + "Z";
}

/** "17 August 2026" style, but written the way the page reads: "August 17, 2026". */
export function formatDay(value: string | null | undefined): string {
  if (!value) return "unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "unknown";
  return `${MONTHS[date.getUTCMonth()]} ${date.getUTCDate()}, ${date.getUTCFullYear()}`;
}

export function relativeTime(value: string | null | undefined, now = Date.now()): string {
  if (!value) return "never observed";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "unknown";
  const seconds = Math.round((now - date.getTime()) / 1000);
  if (seconds < 90) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return `${minutes} minutes ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 36) return `${hours} hours ago`;
  const days = Math.round(hours / 24);
  if (days < 45) return `${days} days ago`;
  return `${Math.round(days / 30)} months ago`;
}

/** The last instant a half-open period actually covers. */
function lastCoveredDay(periodTo: string): Date {
  return new Date(new Date(periodTo).getTime() - 1);
}

/**
 * The ISO week a period really covers.
 *
 * `period_to` is exclusive, so a Monday-aligned window ends at the first instant
 * of the *following* week. Naming a report after that instant labels week 34 as
 * week 35 — which is exactly what the first stored production brief did. The
 * page derives its own label from the period instead of trusting stored prose.
 */
export function isoWeek(periodTo: string): { year: number; week: number } {
  const end = lastCoveredDay(periodTo);
  const date = new Date(
    Date.UTC(end.getUTCFullYear(), end.getUTCMonth(), end.getUTCDate()),
  );
  const day = date.getUTCDay() || 7;
  date.setUTCDate(date.getUTCDate() + 4 - day);
  const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
  const week = Math.ceil(((date.getTime() - yearStart.getTime()) / 86400000 + 1) / 7);
  return { year: date.getUTCFullYear(), week };
}

/** "August 17–23, 2026" — the days the period covers, not its exclusive end. */
export function periodLabel(periodFrom: string, periodTo: string): string {
  const from = new Date(periodFrom);
  const to = lastCoveredDay(periodTo);
  if (Number.isNaN(from.getTime()) || Number.isNaN(to.getTime())) return "unknown period";
  const sameMonth =
    from.getUTCMonth() === to.getUTCMonth() && from.getUTCFullYear() === to.getUTCFullYear();
  if (sameMonth) {
    return `${MONTHS[from.getUTCMonth()]} ${from.getUTCDate()}–${to.getUTCDate()}, ${to.getUTCFullYear()}`;
  }
  return `${formatDay(periodFrom)} – ${MONTHS[to.getUTCMonth()]} ${to.getUTCDate()}, ${to.getUTCFullYear()}`;
}

export function isoWeekLabel(periodTo: string): string {
  const { year, week } = isoWeek(periodTo);
  return `ISO week ${week}, ${year}`;
}

export function scopeLabel(scope: string | null | undefined): string {
  if (!scope) return "unscoped";
  return scope.replace("product/", "").replace("/", " · ");
}

export function entityLabel(entity: string): string {
  return entity
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function bytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export function sourceLabel(source: string): string {
  return source.replace(/_/g, " ");
}

/** "one" / "two" / "three" reads better than a digit in a sentence. */
export function countWord(value: number): string {
  return (
    ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"][value] ??
    String(value)
  );
}

export function plural(value: number, singular: string, pluralForm?: string): string {
  return value === 1 ? singular : (pluralForm ?? `${singular}s`);
}

/** Where a public source lives, said in words rather than a source key. */
export function sourceKindLabel(kind: string): string {
  if (kind === "repository") return "GitHub releases";
  if (kind === "url") return "official changelog";
  return kind;
}
