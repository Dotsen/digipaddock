// Unsent text per person, card and field: it survives leaving the card, a resync and a reload
// of the tab, and goes with a sign-out. sessionStorage may be missing or refuse (a private window): then a
// draft simply lives as long as the field.

const PREFIX = "kd.draft.";

export function draftKey(user: string, card: string, context: string): string {
  return `${PREFIX}${user}.${card}.${context}`;
}

export function readDraft(key: string): string {
  try {
    return sessionStorage.getItem(key) ?? "";
  } catch {
    return "";
  }
}

/** Keep `text` under `key`; an empty text removes the key. */
export function writeDraft(key: string, text: string): void {
  try {
    if (text) sessionStorage.setItem(key, text);
    else sessionStorage.removeItem(key);
  } catch {
    // no storage: the draft lives in the field only
  }
}

const onClear = new Set<() => void>();

/** `fn` runs with every `clearDrafts`: a copy of drafts kept in memory goes with the stored ones. */
export function onDraftsCleared(fn: () => void): void {
  onClear.add(fn);
}

/** Every draft of this tab (a sign-out). */
export function clearDrafts(): void {
  onClear.forEach((fn) => fn());
  try {
    const keys: string[] = [];
    for (let i = 0; i < sessionStorage.length; i += 1) {
      const key = sessionStorage.key(i);
      if (key?.startsWith(PREFIX)) keys.push(key);
    }
    keys.forEach((key) => sessionStorage.removeItem(key));
  } catch {
    // nothing stored
  }
}

/** What stays in the field once `sent` went: the text typed after it while it was sending, or all of
 * `current` when the person rewrote the field meanwhile. */
export function afterSend(current: string, sent: string): string {
  if (current === sent) return "";
  if (current.startsWith(sent)) return current.slice(sent.length).replace(/^\s+/, "");
  return current;
}

/** Ctrl+Enter (Cmd+Enter on a Mac) sends; Enter alone breaks the line; an IME composition never sends. */
export function isSendKey(event: KeyboardEvent): boolean {
  return event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.isComposing;
}
