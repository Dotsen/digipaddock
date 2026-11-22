// Short notices at the bottom of the screen ("the card changed", "saved").

export type ToastKind = "info" | "ok" | "error";

export interface ToastItem {
  id: number;
  text: string;
  kind: ToastKind;
}

const SHOWN_MS = 5000;
const MAX = 3;
let next = 1;

class Toasts {
  items = $state<ToastItem[]>([]);
}

export const toasts = new Toasts();

export function dismiss(id: number) {
  toasts.items = toasts.items.filter((item) => item.id !== id);
}

export function toast(text: string, kind: ToastKind = "info") {
  const id = next++;
  toasts.items = [...toasts.items, { id, text, kind }].slice(-MAX);
  setTimeout(() => dismiss(id), SHOWN_MS);
}
