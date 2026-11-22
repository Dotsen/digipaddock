import { describe, expect, it } from "vitest";
import en from "../../messages/en.json";
import ru from "../../messages/ru.json";
import { currentCard, detectLocale, errorText, getLocale, setLocale, setNames, t } from "./i18n.svelte";
import { ApiError } from "./api";

describe("i18n", () => {
  it("has the same keys in both catalogs", () => {
    expect(Object.keys(en).sort()).toEqual(Object.keys(ru).sort());
  });

  it("fills parameters and follows the locale", () => {
    setLocale("ru");
    expect(t("inbox.selected", { count: 3 })).toBe("выбрано: 3");
    setLocale("en");
    expect(getLocale()).toBe("en");
    expect(t("nav.inbox")).toBe(en["nav.inbox"]);
    setLocale("ru");
  });

  it("names the servers section in both languages (KD-9055)", () => {
    const keys = ["nav.servers", "server.check_now", "incident.ack", "incident.mute", "incident.unmute", "overview.servers",
      "incident.mute.1h", "incident.mute.1d", "incident.mute.7d", "server.check.state.queued",
      "server.check.state.running", "server.check.state.done", "server.check.state.failed"] as const;
    for (const key of keys) {
      expect(ru[key], key).toBeTruthy();
      expect(en[key], key).toBeTruthy();
    }
    setLocale("ru");
    expect(t("nav.servers")).toBe("Серверы");
    setLocale("en");
    expect(t("nav.servers")).toBe("Servers");
    setLocale("ru");
    expect(Object.keys(ru).filter((key) => key.startsWith("overview.monitor"))).toEqual([]);
  });

  it("picks Russian for ru* browsers and English otherwise", () => {
    expect(detectLocale("ru-RU")).toBe("ru");
    expect(detectLocale("ru")).toBe("ru");
    expect(detectLocale("en-GB")).toBe("en");
    expect(detectLocale("de")).toBe("en");
    expect(detectLocale(undefined)).toBe("en");
  });

  it("describes an error by its kind", () => {
    setLocale("ru");
    expect(errorText(new ApiError(409, "conflict", "stale"))).toBe(ru["error.conflict"]);
    expect(errorText(new ApiError(403, "refused", "no"))).toBe(ru["error.forbidden"]);
    expect(errorText(new ApiError(418, "teapot", "?"))).toBe(ru["error.unknown"]);
    expect(errorText(new Error("boom"))).toBe(ru["error.unknown"]);
  });

  it("names the server's reason before the kind (KD-9221)", () => {
    // a BOSS approval refused while a release reinstalls BOSS read as «the card changed»
    setLocale("ru");
    const off = new ApiError(503, "unavailable", "канал BOSS выключен", { reason: "boss_off" });
    expect(errorText(off)).toBe(ru["error.reason.boss_off"]);
    const expired = new ApiError(409, "conflict", "срок истёк", { reason: "proposal_expired" });
    expect(errorText(expired)).toBe(ru["error.reason.proposal_expired"]);
    expect(errorText(new ApiError(409, "conflict", "x", { reason: "no_such_code" }))).toBe(ru["error.conflict"]);
  });

  it("names the host's commands and cards in a catalog text, never in a parameter", () => {
    setLocale("en");
    setNames("acme", "AC");
    try {
      const key = Object.keys(en).find((k) => (en as Record<string, string>)[k].includes("kd-session-host resume"));
      expect(key).toBeTruthy();
      expect(t(key as never, { failed: "kd-task" })).toContain("acme-session-host resume");
      expect(t(key as never, { failed: "kd-task" })).not.toContain("kd-session-host");
      expect(t("inbox.selected", { count: "kd-x" })).toContain("kd-x");
    } finally {
      setNames();
    }
    expect(t("inbox.selected", { count: 1 })).not.toContain("acme");
  });

  it("opens an old link's card under the host's current prefix", () => {
    setNames("kd", "KD", ["OLD"]);
    try {
      expect(currentCard("OLD-12")).toBe("KD-12");
    } finally {
      setNames();
    }
    expect(currentCard("OLD-12")).toBe("OLD-12");
  });
});
