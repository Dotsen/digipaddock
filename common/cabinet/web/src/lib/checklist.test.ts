import { describe, expect, it } from "vitest";
import { appendTaskLine, parseTaskLines, toggleTaskLine } from "./checklist";

const DOD = "- [ ] #1 Тесты зелёные\n- [x] #2 Ревью пройдено\n\nЗаметка между пунктами.\n- [ ] #3 Выкачено на stage";

describe("parseTaskLines", () => {
  it("splits items from the markdown between them", () => {
    expect(parseTaskLines(DOD)).toEqual([
      { type: "item", line: 0, checked: false, text: "#1 Тесты зелёные" },
      { type: "item", line: 1, checked: true, text: "#2 Ревью пройдено" },
      { type: "text", text: "\nЗаметка между пунктами." },
      { type: "item", line: 4, checked: false, text: "#3 Выкачено на stage" },
    ]);
  });

  it("accepts * bullets, indentation and a capital X", () => {
    expect(parseTaskLines("  * [X] one\n* [ ] two")).toEqual([
      { type: "item", line: 0, checked: true, text: "one" },
      { type: "item", line: 1, checked: false, text: "two" },
    ]);
  });

  it("leaves lines that only look like tasks as text", () => {
    expect(parseTaskLines("-[ ] no space\n- [] empty\n- [ ]no gap")).toEqual([
      { type: "text", text: "-[ ] no space\n- [] empty\n- [ ]no gap" },
    ]);
  });

  it("gives nothing for an empty text", () => {
    expect(parseTaskLines("")).toEqual([]);
  });
});

describe("toggleTaskLine", () => {
  it("rewrites only that line's box", () => {
    const once = toggleTaskLine(DOD, 0);
    expect(once.split("\n")[0]).toBe("- [x] #1 Тесты зелёные");
    expect(once.split("\n").slice(1)).toEqual(DOD.split("\n").slice(1));
    expect(toggleTaskLine(once, 0)).toBe(DOD);
    expect(toggleTaskLine("* [X] done", 0)).toBe("* [ ] done");
  });

  it("keeps Windows line ends and ignores a line that is not a task", () => {
    expect(toggleTaskLine("- [ ] a\r\n- [ ] b\r\n", 1)).toBe("- [ ] a\r\n- [x] b\r\n");
    expect(toggleTaskLine(DOD, 3)).toBe(DOD);
    expect(toggleTaskLine(DOD, 99)).toBe(DOD);
  });
});

describe("appendTaskLine", () => {
  it("numbers the new item after the highest #n", () => {
    expect(appendTaskLine(DOD, "  Документы обновлены ")).toBe(`${DOD}\n- [ ] #4 Документы обновлены`);
  });

  it("counts items when none is numbered and starts an empty text", () => {
    expect(appendTaskLine("- [x] a\n- [ ] b\n", "c")).toBe("- [x] a\n- [ ] b\n- [ ] #3 c");
    expect(appendTaskLine("", "first")).toBe("- [ ] #1 first");
  });

  it("adds nothing for an empty label", () => {
    expect(appendTaskLine(DOD, "   ")).toBe(DOD);
  });
});
