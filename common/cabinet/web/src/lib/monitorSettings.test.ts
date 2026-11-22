import { describe, expect, it } from "vitest";
import {
  changesOf,
  defaultOf,
  effective,
  fieldId,
  formatValue,
  parseInput,
  sectionOf,
  toggled,
  type CatalogEntry,
  type MonitorSettings,
} from "./monitorSettings";

const NUM: CatalogEntry = { key: "disk_pct.warn", scope: "global", type: "int", min: 1, max: 100, weaker: "up" };
const LIST: CatalogEntry = { key: "units_ignore", scope: "global", type: "list", weaker: "add" };
const QUIET: CatalogEntry = { key: "quiet_hours", scope: "global", type: "quiet", weaker: "wider" };
const MB: CatalogEntry = { key: "mem_available_mb.warn", scope: "server", type: "num", min: 1, max: 1000000, weaker: "down" };

const settings = (over: Partial<MonitorSettings> = {}): MonitorSettings => ({
  revision: 2,
  items: [
    { key: "disk_pct.warn", server: "", value: 80, by: "user:owner", at: "2026-10-23T08:00:00Z", stale: false },
    { key: "every_min.cores", server: "", value: 2880, by: "user:owner", at: "2026-10-23T08:00:00Z", stale: true },
  ],
  defaults: { "disk_pct.warn": 85, "every_min.cores": 1440, units_ignore: ["a"], quiet_hours: { start: "23:00", end: "08:00" } },
  server_defaults: { "node-l": { "mem_available_mb.warn": 100 } },
  catalog: [NUM, LIST, QUIET, MB],
  ...over,
});

describe("the value in force", () => {
  it("is the edit, else config.json's; a stale edit is not applied", () => {
    const s = settings();
    expect(effective(s, "disk_pct.warn")).toBe(80);
    expect(effective(s, "every_min.cores")).toBe(1440);
    expect(effective(s, "units_ignore")).toEqual(["a"]);
    expect(defaultOf(s, "mem_available_mb.warn", "node-l")).toBe(100);
    expect(defaultOf(s, "disabled_checks", "node-l")).toEqual([]);
  });
});

describe("a field's text", () => {
  it("parses numbers within range, lists and quiet hours", () => {
    expect(parseInput(NUM, " 90 ")).toEqual({ value: 90 });
    expect(parseInput(NUM, "90.5")).toBeNull();
    expect(parseInput(NUM, "0")).toBeNull();
    expect(parseInput(NUM, "")).toBeNull();
    expect(parseInput(MB, "80,5")).toEqual({ value: 80.5 });
    expect(parseInput(LIST, "a, b\nc,,")).toEqual({ value: ["a", "b", "c"] });
    expect(parseInput(LIST, "")).toEqual({ value: [] });
    expect(parseInput(QUIET, "22:00–07:30")).toEqual({ value: { start: "22:00", end: "07:30" } });
    expect(parseInput(QUIET, "25:00-07:00")).toBeNull();
  });

  it("round-trips through the field", () => {
    expect(formatValue(QUIET, { start: "23:00", end: "08:00" })).toBe("23:00–08:00");
    expect(formatValue(LIST, ["a", "b"])).toBe("a, b");
    expect(formatValue(NUM, undefined)).toBe("");
  });
});

describe("a save's changes", () => {
  it("are the fields whose value differs from the one in force", () => {
    const s = settings();
    const { changes, bad } = changesOf(s, {
      [fieldId("disk_pct.warn")]: "80", // unchanged
      [fieldId("units_ignore")]: "a, b",
      [fieldId("quiet_hours")]: "23:00 - 08:00", // the same value written otherwise
      [fieldId("mem_available_mb.warn", "node-l")]: "90",
    });
    expect(bad).toEqual([]);
    expect(changes).toEqual([
      { key: "units_ignore", value: ["a", "b"] },
      { key: "mem_available_mb.warn", server: "node-l", value: 90 },
    ]);
  });

  it("stop on a field that is not a value", () => {
    expect(changesOf(settings(), { [fieldId("disk_pct.warn")]: "lots" }).bad).toEqual(["disk_pct.warn|"]);
  });
});

it("puts keys in sections and toggles checks", () => {
  expect(sectionOf("security.load.warn_x")).toBe("security");
  expect(sectionOf("quiet_hours")).toBe("wake");
  expect(sectionOf("every_min.certs")).toBe("every");
  expect(toggled(["disk"], "certs", false)).toEqual(["disk", "certs"]);
  expect(toggled(["disk", "certs"], "disk", true)).toEqual(["certs"]);
});
