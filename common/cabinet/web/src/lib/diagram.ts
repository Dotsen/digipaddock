// A diagram in a card's text: a ```mermaid block is drawn by mermaid in a frame of its own, /diagram.html
// (vite.config.ts builds it from src/diagram/). The frame is sandboxed and has its own Content-Security-Policy, so
// the page and the frame only exchange messages; what comes back is the word of a document the cabinet does not
// trust: a height and a line of text are read, nothing else.

export const FRAME = "/diagram.html";
export const ANSWER_MS = 15_000; // a frame that did not answer in this time: the block is shown as its text
const MAX_HEIGHT = 4000; // a taller drawing scrolls inside its frame
const MAX_WHY = 400;

export type Scheme = "dark" | "light";
export type Theme = Record<string, string | boolean>;
// `id` numbers the requests of one frame: an answer says which text it is about
export type Render = { type: "render"; id: number; code: string; scheme: Scheme; theme: Theme };
export type Answer = { type: "rendered"; id: number; height: number } | { type: "error"; id: number; message: string };

// mermaid's «base» theme variable → the cabinet's token it takes its value from
const TOKENS: Record<string, string> = {
  primaryColor: "--surface-2",
  primaryTextColor: "--text",
  primaryBorderColor: "--muted",
  lineColor: "--muted",
  secondaryColor: "--surface-3",
  tertiaryColor: "--surface",
  noteBkgColor: "--surface-3",
  noteTextColor: "--text",
  noteBorderColor: "--line",
  fontFamily: "--font-ui",
};

/** The cabinet's palette as mermaid's theme variables; a token the page does not define is left to mermaid.
 *  `backdrop` is the colour behind the drawing: the frame is see-through, and a label covers its line with it. */
export function themeOf(token: (name: string) => string, scheme: Scheme, backdrop = ""): Theme {
  const theme: Theme = { darkMode: scheme === "dark", fontSize: "14px" };
  for (const [variable, name] of Object.entries(TOKENS)) {
    const value = token(name).trim();
    if (value) theme[variable] = value;
  }
  const behind = backdrop || token("--bg").trim();
  if (behind) theme.background = theme.edgeLabelBackground = behind;
  return theme;
}

/** The nearest solid background behind an element — a message's tint, a card's surface, the page. */
function backdropOf(at: Element): string {
  for (let up: Element | null = at; up; up = up.parentElement) {
    const colour = getComputedStyle(up).backgroundColor;
    if (/^rgb\(\d+, \d+, \d+\)$/.test(colour)) return colour;
  }
  return "";
}

/** What the frame is asked: the block's text, to be drawn in the colours of the place it stands in. */
export function renderMessage(id: number, code: string, at: Element): Render {
  const scheme: Scheme = document.documentElement.dataset.theme === "light" ? "light" : "dark";
  const style = getComputedStyle(at);
  const theme = themeOf((name) => style.getPropertyValue(name), scheme, backdropOf(at));
  return { type: "render", id, code, scheme, theme };
}

/** The frame's answer, or null for anything else that reaches the window. */
export function answerOf(data: unknown): Answer | null {
  if (typeof data !== "object" || data === null) return null;
  const { type, id, height, message } = data as Record<string, unknown>;
  if (typeof id !== "number") return null;
  if (type === "rendered" && typeof height === "number" && Number.isFinite(height) && height > 0) {
    return { type, id, height: Math.min(Math.ceil(height), MAX_HEIGHT) };
  }
  if (type === "error") return { type, id, message: typeof message === "string" ? message.slice(0, MAX_WHY) : "" };
  return null;
}
