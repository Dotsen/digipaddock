// A role's session context limit (Settings → Models → «Context limit», kd.cabinet.context_limits): a whole number of
// tokens in Claude Code's range or the explicit «no limit»; and the session's compaction line, whose sizes are tokens.
import { getLocale } from "./i18n.svelte";

export const LIMIT_MIN = 100_000;
export const LIMIT_MAX = 1_000_000;
export const LIMIT_DEFAULT = 400_000;
export const NO_LIMIT = "none";

export type Limit = number | typeof NO_LIMIT;

/** One role's row: its limit in force and whether the owner set it (else the default). */
export interface ContextLimit {
  agent: string;
  limit: Limit;
  set: boolean;
}

/** What the owner typed as tokens, or null when it is not a whole number from LIMIT_MIN to LIMIT_MAX. Spaces
 * between the digit groups are allowed ("400 000"); anything else — a fraction, a letter, an empty field — is not. */
export function parseLimit(text: string): number | null {
  const plain = String(text ?? "").replace(/\s/g, ""); // \s takes the no-break spaces of a pasted "400 000" too
  if (!/^\d+$/.test(plain)) return null;
  const value = Number(plain);
  return Number.isSafeInteger(value) && value >= LIMIT_MIN && value <= LIMIT_MAX ? value : null;
}

/** A token count with the reader's digit groups: 400000 → "400 000" (ru), "400,000" (en). */
export function tokens(n: number): string {
  return n.toLocaleString(getLocale() === "ru" ? "ru-RU" : "en-GB");
}

const SIZES = ["before", "after"];

/** A status line's parameters as the reader sees them: a compaction's sizes with digit groups. */
export function statusParams(key: string, params: Record<string, unknown> | undefined): Record<string, string | number> {
  const out: Record<string, string | number> = {};
  for (const [name, value] of Object.entries(params ?? {})) {
    const size = key.startsWith("sys.session.compacted") && SIZES.includes(name) && typeof value === "number";
    out[name] = size ? tokens(value as number) : (value as string | number);
  }
  return out;
}
