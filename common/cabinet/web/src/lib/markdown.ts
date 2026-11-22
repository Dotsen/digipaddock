// Markdown of cards and messages: `marked` (GFM) with raw HTML shown as text, then DOMPurify with a
// small tag list. Links leave the cabinet in a new tab; javascript: and other schemes are dropped.
// A ```mermaid block of its own is not HTML at all: its text goes to the diagram frame (lib/diagram.ts).
import DOMPurify from "dompurify";
import { Marked, type Token, type Tokens } from "marked";
import { getNames } from "./i18n.svelte";
import { canonicalCard } from "./names";

const escape = (text: string) =>
  text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

const marked = new Marked({
  gfm: true,
  breaks: true,
  renderer: {
    html(token: Tokens.HTML | Tokens.Tag) {
      return escape(token.text);
    },
  },
});

const PROFILE = {
  ALLOWED_TAGS: [
    "a", "b", "blockquote", "br", "code", "del", "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i",
    "input", "li", "ol", "p", "pre", "s", "strong", "table", "tbody", "td", "th", "thead", "tr", "ul",
  ],
  ALLOWED_ATTR: ["href", "title", "type", "checked", "disabled", "align"],
  ALLOWED_URI_REGEXP: /^(?:https?:|mailto:|\/(?!\/)|#)/i,
};

let hooked = false;

function purify(html: string): string {
  if (!hooked) {
    DOMPurify.addHook("afterSanitizeAttributes", (node) => {
      if (node.tagName !== "A") return;
      const href = node.getAttribute("href") ?? "";
      if (/^https?:/i.test(href)) {
        node.setAttribute("target", "_blank");
        node.setAttribute("rel", "noopener noreferrer");
      }
    });
    hooked = true;
  }
  return DOMPurify.sanitize(html, { ...PROFILE, ADD_ATTR: ["target", "rel"] }) as string;
}

// A card number in plain text is a link to its card: agents write only `KD-<n>`, no address. The id
// prefix is the host's (`/api/me`), an earlier one of the host links to the same card; a number inside a
// word, a path or a query is not a mention.
const mentions = new Map<string, RegExp>();
const NO_LINKS = new Set(["A", "CODE", "PRE"]);

function cardMention(prefixes: string[]): RegExp {
  const key = prefixes.join("|");
  let found = mentions.get(key);
  if (!found) {
    found = new RegExp(`(?<![\\p{L}\\p{N}_/#=?&-])(?:${key})-([0-9]{1,6})(?![\\p{L}\\p{N}_-])`, "gu");
    mentions.set(key, found);
  }
  return found;
}

const prefixes = () => [getNames().cards, ...(getNames().aliases ?? [])];

function linkText(node: Text): void {
  const text = node.data;
  const matches = [...text.matchAll(cardMention(prefixes()))];
  if (!matches.length) return;
  const out = document.createDocumentFragment();
  let last = 0;
  for (const match of matches) {
    const at = match.index ?? 0;
    if (at > last) out.append(text.slice(last, at));
    const link = document.createElement("a");
    link.setAttribute("href", `/cards/${canonicalCard(match[0], getNames())}`);
    link.textContent = match[0];
    out.append(link);
    last = at + match[0].length;
  }
  if (last < text.length) out.append(text.slice(last));
  node.replaceWith(out);
}

function linkCards(html: string): string {
  if (!prefixes().some((prefix) => html.includes(`${prefix}-`))) return html;
  const box = document.createElement("template");
  box.innerHTML = html;
  const texts: Text[] = [];
  const walker = document.createTreeWalker(box.content, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    let inside = false;
    for (let up = node.parentElement; up && !inside; up = up.parentElement) inside = NO_LINKS.has(up.tagName);
    if (!inside) texts.push(node as Text);
  }
  texts.forEach(linkText);
  return box.innerHTML;
}

export function renderMarkdown(text: string): string {
  return linkCards(purify(marked.parse(text ?? "", { async: false }) as string));
}

export type Part = { kind: "html"; html: string } | { kind: "diagram"; code: string };

const isDiagram = (token: Token): token is Tokens.Code =>
  token.type === "code" && /^mermaid(\s|$)/i.test(token.lang ?? "") && token.text.trim() !== "";

// every diagram is a frame that loads mermaid: a text draws its first few, the rest stay code blocks
export const MAX_DIAGRAMS = 8;

/** A text as the parts the cabinet shows: sanitized HTML and, between its pieces, the diagrams — each a
 *  ```mermaid block at the text's top level (one inside a list or a quote stays a code block). */
export function renderParts(text: string): Part[] {
  const parts: Part[] = [];
  let run: Token[] = [];
  let drawn = 0;
  const flush = () => {
    const html = linkCards(purify(marked.parser(run)));
    if (html.trim()) parts.push({ kind: "html", html });
    run = [];
  };
  for (const token of marked.lexer(text ?? "")) {
    if (isDiagram(token) && drawn < MAX_DIAGRAMS) {
      flush();
      drawn += 1;
      parts.push({ kind: "diagram", code: token.text });
    } else {
      run.push(token);
    }
  }
  flush();
  return parts;
}

export function renderInline(text: string): string {
  return linkCards(purify(marked.parseInline(text ?? "", { async: false }) as string));
}
