// A unified git diff read into files, hunks and numbered lines: what the PR decision shows and what a
// line remark points at. The diff may be cut at a line boundary: a file or a hunk that ends early keeps what came.
// Inside a hunk its header's line counts say which lines are content, so a removed «-- x» is not a file header.

export interface DiffLine {
  kind: "add" | "del" | "ctx";
  old?: number;
  new?: number;
  text: string;
}

export interface DiffHunk {
  header: string;
  lines: DiffLine[];
}

export interface DiffFile {
  path: string;
  /** The path before the change; "" for a new file. */
  oldPath: string;
  binary: boolean;
  hunks: DiffHunk[];
}

/** A remark on one line: the new side for an added or kept line, the old side for a removed one. */
export interface LineComment {
  path: string;
  line: number;
  side: "new" | "old";
  body: string;
}

/** The backend's limits on remarks: the text the thread records, and line remarks in one Forgejo review. */
export const REMARKS_MAX = 4000;
export const COMMENTS_MAX = 30;

/** The remarks as the thread records them (the backend composes the same): the text, then a line per remark. */
export function remarksText(text: string, comments: LineComment[]): string {
  const lines = comments.map((c) => `- \`${c.path}:${c.line}\` — ${c.body}`).join("\n");
  return [text.trim(), lines].filter(Boolean).join("\n\n");
}

// Characters a reader cannot see but a compiler or a browser acts on: bidi controls that reorder what is
// shown, zero-width and tag characters, other control and format characters (the tab and a line's CR excepted).
// The same set as the dev agent's file-name quoting (`forge._shown`).
const HIDDEN = /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu;
const SEEN = new Set(["\t", "\r"]);
const codeOf = (char: string) => "U+" + (char.codePointAt(0) ?? 0).toString(16).toUpperCase().padStart(4, "0");

/** A line cut at its hidden characters: the plain pieces and, between them, each hidden one as its code point. */
export function splitHidden(text: string): Array<{ text: string } | { hidden: string }> {
  const parts: Array<{ text: string } | { hidden: string }> = [];
  let last = 0;
  for (const match of text.matchAll(HIDDEN)) {
    if (SEEN.has(match[0])) continue;
    const at = match.index ?? 0;
    if (at > last) parts.push({ text: text.slice(last, at) });
    parts.push({ hidden: codeOf(match[0]) });
    last = at + match[0].length;
  }
  if (last < text.length || !parts.length) parts.push({ text: text.slice(last) });
  return parts;
}

/** The code points of a text's hidden characters, in order. */
export const hiddenCodes = (text: string): string[] =>
  splitHidden(text).flatMap((part) => ("hidden" in part ? [part.hidden] : []));

const HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;
const QUOTED = /^"(?:[^"\\]|\\.)*"$/;
const ESCAPES: Record<string, number> = { a: 7, b: 8, t: 9, n: 10, v: 11, f: 12, r: 13, '"': 34, "\\": 92 };

/** A path as git writes it: in quotes with C escapes (octal bytes of UTF-8) when it has unusual characters. */
export function unquote(path: string): string {
  if (!QUOTED.test(path)) return path;
  const bytes: number[] = [];
  const encoder = new TextEncoder();
  for (const part of path.slice(1, -1).split(/(\\(?:[0-7]{1,3}|.))/)) {
    if (part.length > 1 && part[0] === "\\") {
      const e = part.slice(1);
      bytes.push(/^[0-7]+$/.test(e) ? parseInt(e, 8) & 255 : (ESCAPES[e] ?? e.charCodeAt(0)));
    } else bytes.push(...encoder.encode(part));
  }
  return new TextDecoder().decode(new Uint8Array(bytes));
}

function strip(path: string, prefix: string): string {
  return path.startsWith(prefix) ? path.slice(prefix.length) : path;
}

/** The old and new path of a «diff --git» line; either may be quoted. */
function headerPaths(rest: string): [string, string] {
  const lead = /^("(?:[^"\\]|\\.)*") (.+)$/.exec(rest);
  const tail = /^(.+) ("(?:[^"\\]|\\.)*")$/.exec(rest);
  const plain = /^(.+) (b\/.+)$/.exec(rest);
  const [a, b] = lead ? [lead[1], lead[2]] : tail ? [tail[1], tail[2]] : plain ? [plain[1], plain[2]] : [rest, rest];
  return [strip(unquote(a), "a/"), strip(unquote(b), "b/")];
}

/** A «---»/«+++» path: quoted or not, with the tab git adds after a name with a space. */
function sidePath(text: string, prefix: string): string {
  return strip(unquote(text.replace(/\t$/, "")), prefix);
}

export function parseDiff(text: string): DiffFile[] {
  const files: DiffFile[] = [];
  let file: DiffFile | null = null;
  let hunk: DiffHunk | null = null;
  let oldAt = 0;
  let newAt = 0;
  let oldLeft = 0;
  let newLeft = 0;
  const lines = text.split("\n");
  if (lines[lines.length - 1] === "") lines.pop(); // the newline that ends the last line, not an empty line
  for (const line of lines) {
    if (hunk && (oldLeft > 0 || newLeft > 0)) {
      const sign = line[0];
      if (sign === "\\") continue; // «\ No newline at end of file»
      if (sign === "+") {
        hunk.lines.push({ kind: "add", new: newAt++, text: line.slice(1) });
        newLeft--;
        continue;
      }
      if (sign === "-") {
        hunk.lines.push({ kind: "del", old: oldAt++, text: line.slice(1) });
        oldLeft--;
        continue;
      }
      if (sign === " " || line === "") {
        hunk.lines.push({ kind: "ctx", old: oldAt++, new: newAt++, text: line.slice(1) });
        oldLeft--;
        newLeft--;
        continue;
      }
      // anything else ends the hunk early: read it as a header below
    }
    hunk = null;
    if (line.startsWith("diff --git ")) {
      const [oldPath, path] = headerPaths(line.slice("diff --git ".length));
      file = { path, oldPath, binary: false, hunks: [] };
      files.push(file);
      continue;
    }
    if (!file) continue;
    const at = HUNK.exec(line);
    if (at) {
      oldAt = Number(at[1]);
      newAt = Number(at[3]);
      oldLeft = at[2] === undefined ? 1 : Number(at[2]);
      newLeft = at[4] === undefined ? 1 : Number(at[4]);
      hunk = { header: line, lines: [] };
      file.hunks.push(hunk);
    } else if (line.startsWith("new file mode")) {
      file.oldPath = "";
    } else if (line.startsWith("rename from ")) {
      file.oldPath = unquote(line.slice("rename from ".length));
    } else if (line.startsWith("rename to ")) {
      file.path = unquote(line.slice("rename to ".length));
    } else if (line.startsWith("--- ")) {
      file.oldPath = line === "--- /dev/null" ? "" : sidePath(line.slice(4), "a/");
    } else if (line.startsWith("+++ ") && line !== "+++ /dev/null") {
      file.path = sidePath(line.slice(4), "b/");
    } else if (line.startsWith("Binary files ") || line === "GIT binary patch") {
      file.binary = true;
    }
  }
  return files;
}
