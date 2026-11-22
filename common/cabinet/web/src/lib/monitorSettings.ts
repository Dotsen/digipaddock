// The monitor's settings over config.json: what GET /api/monitor-settings answers and how the page turns
// a field's text into a value and back. Plain code without the store or i18n, so the page and the tests share it.

export type EntryType = "int" | "num" | "list" | "quiet" | "checks";

export interface CatalogEntry {
  key: string;
  scope: "global" | "server";
  type: EntryType;
  min?: number;
  max?: number;
  weaker: string;
  checks?: string[];
}

export interface SettingItem {
  key: string;
  server: string;
  value: unknown;
  by: string;
  at: string;
  stale?: boolean;
}

export interface MonitorSettings {
  timezone?: string;
  revision: number;
  items: SettingItem[];
  defaults?: Record<string, unknown>;
  server_defaults?: Record<string, Record<string, unknown>>;
  servers?: string[];
  checks?: Record<string, string[]>;
  catalog?: CatalogEntry[];
}

export type Change = { key: string; server?: string; value: unknown } | { key: string; server?: string; reset: true };

export const OFF = "disabled_checks";

export const fieldId = (key: string, server = ""): string => `${key}|${server}`;

/** The owner's edit of a key (of a server), stale or not. */
export function edited(s: MonitorSettings, key: string, server = ""): SettingItem | undefined {
  return s.items.find((i) => i.key === key && (i.server || "") === server);
}

/** config.json's value of a key; a server's switched-off checks default to none. */
export function defaultOf(s: MonitorSettings, key: string, server = ""): unknown {
  if (!server) return s.defaults?.[key];
  if (key === OFF) return [];
  return s.server_defaults?.[server]?.[key];
}

/** The value in force: the edit, unless the release no longer has its key; else config.json's. */
export function effective(s: MonitorSettings, key: string, server = ""): unknown {
  const edit = edited(s, key, server);
  return edit && !edit.stale ? edit.value : defaultOf(s, key, server);
}

const HHMM = /^([01][0-9]|2[0-3]):[0-5][0-9]$/;

/** A field's text as the value the cabinet takes, or null when it is not one (the page says so before saving). */
export function parseInput(entry: CatalogEntry, text: string): { value: unknown } | null {
  const raw = text.trim();
  switch (entry.type) {
    case "int":
    case "num": {
      if (!raw) return null;
      const n = Number(raw.replace(",", "."));
      if (!Number.isFinite(n) || (entry.type === "int" && !Number.isInteger(n))) return null;
      if ((entry.min !== undefined && n < entry.min) || (entry.max !== undefined && n > entry.max)) return null;
      return { value: n };
    }
    case "list":
      return { value: raw.split(/[\n,]/).map((x) => x.trim()).filter(Boolean) };
    case "quiet": {
      const parts = raw.split(/\s*[-–—]\s*/);
      if (parts.length !== 2 || !HHMM.test(parts[0]) || !HHMM.test(parts[1])) return null;
      return { value: { start: parts[0], end: parts[1] } };
    }
    default:
      return null;
  }
}

/** A value as the field shows it; none — empty. */
export function formatValue(entry: CatalogEntry, value: unknown): string {
  if (value === undefined || value === null) return "";
  if (entry.type === "list" || entry.type === "checks") return Array.isArray(value) ? value.join(", ") : "";
  if (entry.type === "quiet") {
    const q = value as { start?: string; end?: string };
    return q.start && q.end ? `${q.start}–${q.end}` : "";
  }
  return String(value);
}

export const sameValue = (a: unknown, b: unknown): boolean => JSON.stringify(a) === JSON.stringify(b);

export const SECTIONS = ["wake", "resources", "units", "services", "security", "every"] as const;
export type Section = (typeof SECTIONS)[number];

const BY_FIRST: Record<string, Section> = {
  quiet_hours: "wake", max_wakes_per_hour: "wake", unreachable_after: "wake", critical_unreachable: "wake",
  disk_pct: "resources", mem_available_pct: "resources", mem_psi: "resources", swap_io: "resources",
  units_ignore: "units", units_confirm: "units", units_required_after: "units",
  wg_stale_min: "services", heartbeat: "services", mysql: "services", certs: "services", backup: "services",
  monitoring: "services", security: "security", every_min: "every",
};

/** The page's section of a global key: by its first part (`security.load.warn_x` → security); unknown — services. */
export const sectionOf = (key: string): Section => BY_FIRST[key.split(".")[0]] ?? "services";

/** The changes of a save: each field whose text differs from the value in force; a field that is not a value
 * stops the save (its id in `bad`). */
export function changesOf(
  s: MonitorSettings,
  texts: Record<string, string>,
): { changes: Change[]; bad: string[] } {
  const changes: Change[] = [];
  const bad: string[] = [];
  for (const [id, text] of Object.entries(texts)) {
    const [key, server = ""] = id.split("|");
    const entry = s.catalog?.find((e) => e.key === key);
    if (!entry) continue;
    const current = effective(s, key, server);
    if (text === formatValue(entry, current)) continue;
    const parsed = parseInput(entry, text);
    if (!parsed) {
      bad.push(id);
      continue;
    }
    if (sameValue(parsed.value, current)) continue;
    changes.push(server ? { key, server, value: parsed.value } : { key, value: parsed.value });
  }
  return { changes, bad };
}

/** The switched-off checks of a server with `check` turned on or off. */
export function toggled(off: string[], check: string, on: boolean): string[] {
  const rest = off.filter((c) => c !== check);
  return on ? rest : [...rest, check];
}
