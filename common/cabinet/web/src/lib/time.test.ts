import { beforeEach, describe, expect, it } from "vitest";
import { setLocale } from "./i18n.svelte";
import { age, countdown, dayTime, remaining, stamp } from "./time";

const NOW = Date.parse("2026-10-15T12:00:00Z");

beforeEach(() => setLocale("ru"));

describe("time", () => {
  it("says how long ago in minutes, hours or days", () => {
    expect(age("2026-10-15T11:48:00Z", NOW)).toBe("12 мин");
    expect(age("2026-10-15T09:00:00Z", NOW)).toBe("3 ч");
    expect(age("2026-10-12T12:00:00Z", NOW)).toBe("3 д");
    expect(age("2026-10-15T12:00:30Z", NOW)).toBe("0 мин");
  });

  it("says how long is left, or nothing once the term passed", () => {
    expect(remaining("2026-10-15T12:30:00Z", NOW)).toBe("30 мин");
    expect(remaining("2026-10-18T12:00:00Z", NOW)).toBe("3 д");
    expect(remaining("2026-10-15T11:00:00Z", NOW)).toBe("");
    expect(remaining("", NOW)).toBe("");
  });

  it("counts a sign-in code down in minutes and seconds", () => {
    expect(countdown("2026-10-15T12:15:00Z", NOW)).toBe("15:00");
    expect(countdown("2026-10-15T12:00:05Z", NOW)).toBe("0:05");
    expect(countdown("2026-10-15T12:00:00.400Z", NOW)).toBe("0:01");
    expect(countdown("2026-10-15T12:00:00Z", NOW)).toBe("");
    expect(countdown("", NOW)).toBe("");
  });

  it("stamps a message with its time, an older one with its day too", () => {
    const now = new Date(2026, 9, 18, 20, 5).getTime();
    const at = (...parts: [number, number, number, number, number]) => new Date(...parts).toISOString();
    expect(stamp(at(2026, 9, 18, 9, 7), now)).toBe("09:07");
    expect(stamp(at(2026, 9, 17, 23, 59), now)).toBe("вчера, 23:59");
    expect(stamp(at(2026, 9, 3, 14, 32), now)).toMatch(/^3 окт\.?, 14:32$/);
    expect(stamp(at(2025, 11, 31, 8, 0), now)).toBe("31.12.2025, 08:00");
    expect(stamp("", now)).toBe("");
    setLocale("en");
    expect(stamp(at(2026, 9, 17, 23, 59), now)).toBe("yesterday, 23:59");
    expect(stamp(at(2026, 9, 3, 14, 32), now)).toBe("3 Oct, 14:32");
  });

  it("names a deferred card's moment as day.month hour:minute in the person's zone (KD-9236)", () => {
    expect(dayTime("2026-11-08T21:30:00Z", "Etc/GMT-3")).toBe("09.11 00:30");
    expect(dayTime("2026-11-08T21:30:00Z", "UTC")).toBe("08.11 21:30");
    expect(dayTime("")).toBe("");
    expect(dayTime("2026-11-08T21:30:00Z", "Nowhere/Zone")).toMatch(/^\d\d\.\d\d \d\d:\d\d$/); // the device's zone
  });
});
