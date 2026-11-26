// A role's context limit as the owner types it, and a compaction line's sizes as the reader reads them.
import { afterEach, expect, it } from "vitest";
import { setLocale } from "./i18n.svelte";
import { parseLimit, statusParams, tokens } from "./contextLimit";

afterEach(() => setLocale("ru"));

it("takes a whole number of tokens from 100 000 to 1 000 000, digit groups allowed", () => {
  expect(parseLimit("400000")).toBe(400000);
  expect(parseLimit(" 400 000 ")).toBe(400000);
  expect(parseLimit("400 000")).toBe(400000);
  expect(parseLimit("100000")).toBe(100000);
  expect(parseLimit("1000000")).toBe(1000000);
});

it.each(["99999", "1000001", "0", "-400000", "400000.5", "4e5", "abc", "", "400k"])("refuses %j", (text) => {
  expect(parseLimit(text)).toBeNull();
});

it("writes token counts with the reader's digit groups", () => {
  setLocale("ru");
  expect(tokens(412345).replace(/\s/g, " ")).toBe("412 345");
  setLocale("en");
  expect(tokens(412345)).toBe("412,345");
});

it("formats a compaction's sizes and leaves every other status line's parameters as they are", () => {
  setLocale("en");
  expect(statusParams("sys.session.compacted", { trigger: "auto", before: 412345, after: 45005 })).toEqual({
    trigger: "auto", before: "412,345", after: "45,005",
  });
  expect(statusParams("sys.session.compacted_before", { before: 98765 })).toEqual({ before: "98,765" });
  expect(statusParams("sys.session.model", { model: "or-x", before: 5 })).toEqual({ model: "or-x", before: 5 });
  expect(statusParams("sys.session.compacted_bare", undefined)).toEqual({});
});
