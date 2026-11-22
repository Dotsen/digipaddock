// The new card's form: what a person fills in, the body `POST /api/cards` takes from it, the project
// they used last — remembered per person in this browser — and the text they did not send yet, kept for the tab.
import type { Council } from "./council";
import { draftKey, readDraft, writeDraft } from "./drafts";

export interface NewCardDraft {
  title: string;
  project: string;
  description: string;
  /** null — the project's route; "" — nobody. */
  assignee: string | null;
  labels: string[];
  acceptance: string[];
  parent: string;
  dependencies: string[];
  model: string;
  effort: string;
  council: Council;
}

export const emptyDraft = (project: string): NewCardDraft => ({
  title: "",
  project,
  description: "",
  assignee: null,
  labels: [],
  acceptance: [],
  parent: "",
  dependencies: [],
  model: "",
  effort: "",
  council: {},
});

/** Trimmed, each once, in the order chosen. */
const once = (list: string[]): string[] => [...new Set(list.map((item) => item.trim()).filter(Boolean))];

/** The request's body: the title and the project, and of the rest only what was filled in. */
export function newCardBody(draft: NewCardDraft): Record<string, unknown> {
  const body: Record<string, unknown> = { title: draft.title.trim(), project: draft.project };
  if (draft.description.trim()) body.description = draft.description;
  if (draft.assignee !== null) body.assignee = draft.assignee;
  const labels = once(draft.labels);
  if (labels.length) body.labels = labels;
  const acceptance = draft.acceptance.map((text) => text.trim()).filter(Boolean);
  if (acceptance.length) body.acceptance = acceptance.map((text) => ({ text, checked: false }));
  if (draft.parent.trim()) body.parent = draft.parent.trim();
  const dependencies = once(draft.dependencies);
  if (dependencies.length) body.dependencies = dependencies;
  for (const key of ["model", "effort"] as const) if (draft[key]) body[key] = draft[key];
  if (Object.keys(draft.council).length) body.council = draft.council;
  return body;
}

/** "More" holds something: the form shows it open, so nothing goes with a card unseen. */
export const hasMore = (draft: NewCardDraft): boolean =>
  draft.assignee !== null ||
  Object.keys(draft.council).length > 0 ||
  draft.labels.length + draft.acceptance.length + draft.dependencies.length > 0 ||
  [draft.parent, draft.model, draft.effort].some((value) => value.trim() !== "");

const kept = (user: string) => draftKey(user, "new", "card");
const strings = (value: unknown): value is string[] => Array.isArray(value) && value.every((v) => typeof v === "string");
const TEXTS = ["title", "project", "description", "parent", "model", "effort"] as const;

/** Keep the form as it stands while it has unsent text (a title, a description, criteria): closing the sheet —
 * Escape, a click beside it — loses nothing. Without text nothing is kept: "More" alone is not carried over. */
export function keepDraft(user: string, draft: NewCardDraft): void {
  const text = draft.title.trim() || draft.description.trim() || draft.acceptance.length;
  writeDraft(kept(user), text ? JSON.stringify(draft) : "");
}

/** The form this person left with unsent text in this tab; null when there is none. Only this form's fields are
 * taken: a tab opened before a release may have kept one the form no longer has (a milestone). */
export function keptDraft(user: string): NewCardDraft | null {
  try {
    const saved = JSON.parse(readDraft(kept(user)) || "null");
    const whole =
      saved !== null &&
      typeof saved === "object" &&
      TEXTS.every((name) => typeof saved[name] === "string") &&
      (saved.assignee === null || typeof saved.assignee === "string") &&
      strings(saved.labels) && // a tab from before the change kept them as typed text: not this form's draft
      strings(saved.acceptance) &&
      strings(saved.dependencies) &&
      saved.council !== null &&
      typeof saved.council === "object" &&
      !Array.isArray(saved.council);
    if (!whole) return null;
    return Object.fromEntries(Object.keys(emptyDraft("")).map((name) => [name, saved[name]])) as unknown as NewCardDraft;
  } catch {
    return null; // not a draft of this form
  }
}

const key = (user: string) => `kd.newcard.project.${user}`;

/** The project this person created a card in last; "" when none is remembered (or the storage is closed). */
export function lastProject(user: string): string {
  try {
    return localStorage.getItem(key(user)) ?? "";
  } catch {
    return "";
  }
}

export function rememberProject(user: string, project: string): void {
  try {
    localStorage.setItem(key(user), project);
  } catch {
    // not remembered: the form opens on the board's project next time
  }
}
