// @vitest-environment node
// The design check: sizes, corners, spaces and colours come from app.css's tokens (docs/cabinet-design.md). Every
// number still written in a component's styles is counted per file and kind against design-baseline.json, and the
// count may only go down: a new number fails, and so does a count left higher than the file now has (lower it with
// KD_DESIGN_BASELINE=write npx vitest run src/design.test.ts). A size off the scale is a new token in app.css.
import { describe, expect, it } from "vitest";

const KINDS = ["font-size", "radius", "space", "weight", "colour"] as const;
type Kind = (typeof KINDS)[number];
type Counts = Partial<Record<Kind, number>>;

const PX = /(?<![\w.])-?\d+(?:\.\d+)?px\b/g;
const COLOUR = /#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(/g;
const WEIGHTS = new Set(["400", "500", "600", "700", "normal", "bold", "inherit"]);

/** The numbers a stylesheet writes where a token belongs, by kind. */
export function rawCounts(css: string): Counts {
  const counts: Counts = {};
  const add = (kind: Kind, n: number) => {
    if (n) counts[kind] = (counts[kind] ?? 0) + n;
  };
  const px = (value: string) => (value.match(PX) ?? []).filter((v) => !/^-?0(?:\.0+)?px$/.test(v)).length;
  for (const [, prop, value] of css.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/([a-z-]+)\s*:\s*([^;{}]+)/g)) {
    if (prop === "font-size" || prop === "font") add("font-size", px(value));
    else if (prop.endsWith("-radius")) add("radius", px(value));
    else if (/^(?:gap|row-gap|column-gap|padding(?:-[a-z]+)?|margin(?:-[a-z]+)?)$/.test(prop)) add("space", px(value));
    else if (prop === "font-weight" && !WEIGHTS.has(value.trim())) add("weight", 1);
    add("colour", (value.match(COLOUR) ?? []).length);
  }
  return counts;
}

// the test runs in node; the page's own types know no node, so its file system is named at run time
interface Fs {
  readFileSync(path: URL, encoding: "utf-8"): string;
  readdirSync(path: URL, options: { withFileTypes: true }): { name: string; isDirectory(): boolean }[];
  writeFileSync(path: URL, text: string): void;
}
const SRC = new URL("./", import.meta.url);
const BASELINE = new URL("../design-baseline.json", import.meta.url);
const env = (globalThis as { process?: { env: Record<string, string | undefined> } }).process?.env ?? {};

async function nodeFs(): Promise<Fs> {
  const name = "node:fs";
  return (await import(/* @vite-ignore */ name)) as Fs;
}

function files(fs: Fs, dir: URL): URL[] {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    if (entry.isDirectory()) return files(fs, new URL(`${entry.name}/`, dir));
    return /\.(svelte|css)$/.test(entry.name) ? [new URL(entry.name, dir)] : [];
  });
}

/** The styles of a file: a component's <style> blocks, a stylesheet whole — app.css after its tokens. */
function styles(file: string, text: string): string {
  if (file.endsWith(".svelte")) return [...text.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join("\n");
  return file === "app.css" ? text.slice(text.indexOf("* { box-sizing")) : text;
}

/** The counts of every component and stylesheet under src/, keyed "components/Nav.svelte". */
function measure(fs: Fs): Record<string, Counts> {
  const out: Record<string, Counts> = {};
  const found = files(fs, SRC).map((url) => [decodeURIComponent(url.href.slice(SRC.href.length)), url] as const);
  for (const [file, url] of found.sort(([a], [b]) => a.localeCompare(b))) {
    const counts = rawCounts(styles(file, fs.readFileSync(url, "utf-8")));
    if (Object.keys(counts).length) out[file] = counts;
  }
  return out;
}

describe("rawCounts", () => {
  it("counts the numbers a token belongs in, and not the tokens", () => {
    expect(rawCounts("a { font-size: 13px; font-size: var(--fs-md); font: 600 12.5px/1.2 sans; }")).toEqual({ "font-size": 2 });
    expect(rawCounts("a { padding: 0 var(--sp-8) 9px; margin: -4px 0 0; gap: 0px; }")).toEqual({ space: 2 });
    expect(rawCounts("a { border-radius: 9px 9px 0 0; border-top-left-radius: 50%; }")).toEqual({ radius: 2 });
    expect(rawCounts("a { font-weight: 650; font-weight: 600; }")).toEqual({ weight: 1 });
    expect(rawCounts("a { color: #fff; background: rgba(0, 0, 0, .5); border: 1px solid var(--line); }")).toEqual({ colour: 2 });
    expect(rawCounts("@media (min-width: 900px) { a:hover { top: 6px; width: 30px; } } /* gap: 3px */")).toEqual({});
  });
});

it("writes no new number where a design token belongs, and the baseline follows every one taken away", async () => {
  const fs = await nodeFs();
  const now = measure(fs);
  expect(files(fs, SRC).length, "the components were read").toBeGreaterThan(50);
  if (env.KD_DESIGN_BASELINE === "write") {
    fs.writeFileSync(BASELINE, JSON.stringify(now, null, 2) + "\n");
    return;
  }
  const base: Record<string, Counts> = JSON.parse(fs.readFileSync(BASELINE, "utf-8"));
  const grew: string[] = [];
  const fell: string[] = [];
  for (const file of new Set([...Object.keys(now), ...Object.keys(base)])) {
    for (const kind of KINDS) {
      const [n, b] = [now[file]?.[kind] ?? 0, base[file]?.[kind] ?? 0];
      if (n > b) grew.push(`${file}: ${kind} ${b} → ${n}`);
      if (n < b) fell.push(`${file}: ${kind} ${b} → ${n}`);
    }
  }
  expect(grew, "numbers written where an app.css token belongs (docs/cabinet-design.md)").toEqual([]);
  expect(fell, "fewer numbers than design-baseline.json says: lower it (KD_DESIGN_BASELINE=write)").toEqual([]);
});
