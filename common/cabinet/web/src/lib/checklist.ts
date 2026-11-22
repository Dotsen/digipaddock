// A card's DoD is markdown: `- [ ] text` / `* [x] text` lines are checklist items, everything else is
// markdown between them. Toggling rewrites only that line's box; adding appends `- [ ] #<n> text`.
// (AC are a structured list on the card and need none of this.)

export type Segment =
  | { type: "item"; line: number; checked: boolean; text: string }
  | { type: "text"; text: string };

const TASK = /^(\s*[-*] \[)( |x|X)(\] )(.*)$/;
const NUMBER = /^#(\d+) /;

function lines(text: string): string[] {
  return text.split("\n");
}

export function parseTaskLines(text: string): Segment[] {
  if (!text) return [];
  const out: Segment[] = [];
  let prose: string[] = [];
  const flush = () => {
    if (prose.length) out.push({ type: "text", text: prose.join("\n") });
    prose = [];
  };
  lines(text).forEach((raw, line) => {
    const found = TASK.exec(raw.replace(/\r$/, ""));
    if (!found) {
      prose.push(raw.replace(/\r$/, ""));
      return;
    }
    flush();
    out.push({ type: "item", line, checked: found[2] !== " ", text: found[4] });
  });
  flush();
  return out;
}

export function toggleTaskLine(text: string, lineIndex: number): string {
  const all = lines(text);
  const raw = all[lineIndex];
  if (raw === undefined) return text;
  const cr = raw.endsWith("\r") ? "\r" : "";
  const found = TASK.exec(cr ? raw.slice(0, -1) : raw);
  if (!found) return text;
  all[lineIndex] = `${found[1]}${found[2] === " " ? "x" : " "}${found[3]}${found[4]}${cr}`;
  return all.join("\n");
}

export function appendTaskLine(text: string, label: string): string {
  const clean = label.trim();
  if (!clean) return text;
  const items = parseTaskLines(text).filter((s) => s.type === "item");
  const numbers = items.map((s) => Number(NUMBER.exec(s.text)?.[1] ?? 0));
  const next = Math.max(items.length, ...numbers) + 1;
  const sep = text === "" || text.endsWith("\n") ? "" : "\n";
  return `${text}${sep}- [ ] #${next} ${clean}`;
}

export function taskCounts(text: string): [number, number] {
  const items = parseTaskLines(text).filter((s) => s.type === "item");
  return [items.filter((s) => s.type === "item" && s.checked).length, items.length];
}
