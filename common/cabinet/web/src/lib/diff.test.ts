import { describe, expect, it } from "vitest";
import { hiddenCodes, parseDiff, remarksText, splitHidden } from "./diff";

const DIFF = [
  "diff --git a/common/kd/x.py b/common/kd/x.py",
  "index 111..222 100644",
  "--- a/common/kd/x.py",
  "+++ b/common/kd/x.py",
  "@@ -1,3 +1,4 @@ def f():",
  " a = 1",
  "-b = 2",
  "+b = 3",
  "+c = 4",
  " d = 5",
  "\\ No newline at end of file",
  "diff --git a/old.txt b/new.txt",
  "similarity index 90%",
  "rename from old.txt",
  "rename to new.txt",
  "--- a/old.txt",
  "+++ b/new.txt",
  "@@ -10,2 +10,2 @@",
  "-x",
  "+y",
  " z",
  "",
].join("\n");

describe("parseDiff", () => {
  it("parseDiff splits files, hunks and line numbers", () => {
    const files = parseDiff(DIFF);
    expect(files.map((f) => [f.path, f.oldPath, f.binary, f.hunks.length])).toEqual([
      ["common/kd/x.py", "common/kd/x.py", false, 1],
      ["new.txt", "old.txt", false, 1],
    ]);
    expect(files[0].hunks[0].header).toBe("@@ -1,3 +1,4 @@ def f():");
    expect(files[0].hunks[0].lines).toEqual([
      { kind: "ctx", old: 1, new: 1, text: "a = 1" },
      { kind: "del", old: 2, text: "b = 2" },
      { kind: "add", new: 2, text: "b = 3" },
      { kind: "add", new: 3, text: "c = 4" },
      { kind: "ctx", old: 3, new: 4, text: "d = 5" },
    ]);
    expect(files[1].hunks[0].lines).toEqual([
      { kind: "del", old: 10, text: "x" },
      { kind: "add", new: 10, text: "y" },
      { kind: "ctx", old: 11, new: 11, text: "z" },
    ]);
  });

  it("parseDiff keeps binary and cut files", () => {
    const text = [
      "diff --git a/img.png b/img.png",
      "new file mode 100644",
      "index 0000000..3333333",
      "Binary files /dev/null and b/img.png differ",
      "diff --git a/gone.py b/gone.py",
      "deleted file mode 100644",
      "--- a/gone.py",
      "+++ /dev/null",
      "@@ -1,1 +0,0 @@",
      "-bye",
      "diff --git a/a.py b/a.py",
      "--- a/a.py",
      "+++ b/a.py",
      "@@ -1,3 +1,1 @@",
      "--- not a header",
      "-two",
    ].join("\n");
    const files = parseDiff(text);
    expect(files.map((f) => [f.path, f.oldPath, f.binary])).toEqual([
      ["img.png", "", true],
      ["gone.py", "gone.py", false],
      ["a.py", "a.py", false],
    ]);
    expect(files[0].hunks).toEqual([]);
    expect(files[1].hunks[0].lines).toEqual([{ kind: "del", old: 1, text: "bye" }]);
    // the diff was cut inside this hunk: what came is kept, and a removed «-- …» line is not a header
    expect(files[2].hunks[0].lines).toEqual([
      { kind: "del", old: 1, text: "-- not a header" },
      { kind: "del", old: 2, text: "two" },
    ]);
    expect(parseDiff("")).toEqual([]);
  });

  it("remarksText is the text the thread records, as the backend composes it", () => {
    const comments = [
      { path: "common/kd/x.py", line: 3, side: "new" as const, body: "тут" },
      { path: "docs/x.md", line: 1, side: "old" as const, body: "и тут" },
    ];
    expect(remarksText(" Поправь ", comments)).toBe("Поправь\n\n- `common/kd/x.py:3` — тут\n- `docs/x.md:1` — и тут");
    expect(remarksText("", comments.slice(0, 1))).toBe("- `common/kd/x.py:3` — тут");
    expect(remarksText("  ", [])).toBe("");
  });

  it("parseDiff reads the paths git quotes (octal UTF-8, escapes, a tab after a space)", () => {
    // final review: a Cyrillic file name comes as "a/docs/\320\277.md" and must still be a file of its own
    const text = [
      'diff --git "a/docs/\\320\\277.md" "b/docs/\\320\\277.md"',
      "index 1..2 100644",
      '--- "a/docs/\\320\\277.md"',
      '+++ "b/docs/\\320\\277.md"',
      "@@ -1 +1 @@",
      "-a",
      "+b",
      'diff --git a/x y.txt "b/x \\"q\\".txt"',
      "similarity index 90%",
      "rename from x y.txt",
      'rename to "x \\"q\\".txt"',
      "diff --git a/s p.md b/s p.md",
      "--- a/s p.md\t",
      "+++ b/s p.md\t",
      "@@ -1 +1 @@",
      "-c",
      "+d",
    ].join("\n");
    const files = parseDiff(text);
    expect(files.map((f) => [f.oldPath, f.path])).toEqual([
      ["docs/п.md", "docs/п.md"],
      ["x y.txt", 'x "q".txt'],
      ["s p.md", "s p.md"],
    ]);
    expect(files[0].hunks[0].lines.map((l) => l.text)).toEqual(["a", "b"]);
  });

  it("splits a line at its hidden characters and names them by code point", () => {
    expect(hiddenCodes("plain text, tab	here")).toEqual([]);
    const line = "if (a) \u202E /* \u202C";
    expect(splitHidden(line)).toEqual([
      { text: "if (a) " },
      { hidden: "U+202E" },
      { text: " /* " },
      { hidden: "U+202C" },
    ]);
    expect(hiddenCodes("a\u200Bb\u0007c\u2028\u{E0041}")).toEqual(["U+200B", "U+0007", "U+2028", "U+E0041"]);
    expect(splitHidden("clean")).toEqual([{ text: "clean" }]);
  });
});
