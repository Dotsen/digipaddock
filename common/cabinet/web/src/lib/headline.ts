// A long text as one line of a list: an agent's question in the Inbox shows its first line as a short
// title and folds the rest; the full text is on the card. Markdown marks are dropped, the line is cut at a word.
export const HEADLINE_MAX = 90;

function plain(line: string): string {
  return line
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1") // links and pictures — their text
    .replace(/^\s{0,3}(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)/, "") // heading, quote, list marker
    .replace(/\*\*|__|~~|[*`]/g, "") // emphasis and code marks; a single «_» stays (client_key)
    .replace(/\s+/g, " ")
    .trim();
}

/** The first line of the text that says something, at most `max` characters (cut at a word, with «…»). */
export function headline(text: string, max = HEADLINE_MAX): string {
  const first = text.split("\n").map(plain).find((line) => line !== "") ?? "";
  const chars = Array.from(first); // whole code points: an emoji is never cut in half
  if (chars.length <= max) return first;
  const cut = chars.slice(0, max - 1).join("");
  const space = cut.lastIndexOf(" ");
  return `${(space > max / 2 ? cut.slice(0, space) : cut).trimEnd()}…`;
}

/** Whether the text says more than its headline — then the list offers to unfold it. */
export function hasMore(text: string, max = HEADLINE_MAX): boolean {
  const lines = text.split("\n").map(plain).filter((line) => line !== "");
  return lines.length > 1 || (lines.length === 1 && lines[0].length > max);
}
