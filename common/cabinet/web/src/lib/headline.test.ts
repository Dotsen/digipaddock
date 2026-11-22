import { describe, expect, it } from "vitest";
import { HEADLINE_MAX, hasMore, headline } from "./headline";

describe("headline", () => {
  it("is the first line that says something, without markdown marks", () => {
    expect(headline("\n\n## **Вопрос** по [KD-76](/cards/KD-76)\nдальше текст")).toBe("Вопрос по KD-76");
    expect(headline("> 1. `kd-task` — что делать?")).toBe("1. kd-task — что делать?");
    expect(headline("- пункт")).toBe("пункт");
    expect(headline("client_key или __kd_task__?")).toBe("client_key или kd_task?");
  });

  it("never cuts an emoji in half", () => {
    expect(headline(`${"a".repeat(8)}😀😀😀`, 10)).toBe(`${"a".repeat(8)}😀…`);
  });

  it("cuts a long line at a word and marks the cut", () => {
    const long = "слово ".repeat(400).trim(); // ~2400 characters in one line
    const line = headline(long);
    expect(line.length).toBeLessThanOrEqual(HEADLINE_MAX);
    expect(line.endsWith("слово…")).toBe(true);
  });

  it("cuts a line without spaces too", () => {
    expect(headline("x".repeat(200), 10)).toBe(`${"x".repeat(9)}…`);
  });

  it("is empty for an empty text", () => {
    expect(headline("")).toBe("");
    expect(headline(" \n ** \n")).toBe("");
  });
});

describe("hasMore", () => {
  it("says whether the text goes past its headline", () => {
    expect(hasMore("Один короткий вопрос?")).toBe(false);
    expect(hasMore("Заголовок\n\nтело")).toBe(true);
    expect(hasMore("y".repeat(HEADLINE_MAX + 1))).toBe(true);
    expect(hasMore("")).toBe(false);
  });
});
