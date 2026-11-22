import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { dismiss, toast, toasts } from "./toast.svelte";

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  vi.useRealTimers();
  toasts.items = [];
});

describe("toast", () => {
  it("shows a message and hides it after a while", () => {
    toast("карточка изменилась", "error");
    expect(toasts.items).toMatchObject([{ text: "карточка изменилась", kind: "error" }]);
    vi.advanceTimersByTime(5000);
    expect(toasts.items).toEqual([]);
  });

  it("keeps at most three and can be closed by hand", () => {
    for (const n of [1, 2, 3, 4]) toast(`m${n}`);
    expect(toasts.items.map((item) => item.text)).toEqual(["m2", "m3", "m4"]);
    dismiss(toasts.items[0].id);
    expect(toasts.items.map((item) => item.text)).toEqual(["m3", "m4"]);
  });
});
