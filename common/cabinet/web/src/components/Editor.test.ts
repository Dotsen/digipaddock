import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readDraft, writeDraft } from "../lib/drafts";
import { setLocale } from "../lib/i18n.svelte";
import Editor from "./Editor.svelte";

const KEY = "kd.draft.owner.KD-5.description";

function show(value = "старое", revision = 3) {
  const onsave = vi.fn(async () => true);
  const view = render(Editor, { value, revision, draftKey: KEY, editable: true, onsave });
  return { ...view, onsave };
}

const field = () => screen.getByRole("textbox") as HTMLTextAreaElement;
const button = (name: string) => screen.queryByRole("button", { name });

async function edit(text: string) {
  await fireEvent.click(button("Править")!);
  await fireEvent.input(field(), { target: { value: text } });
}

beforeEach(() => setLocale("ru"));
afterEach(() => {
  cleanup();
  sessionStorage.clear();
});

describe("Editor", () => {
  it("saves against the revision the draft started from", async () => {
    const { onsave } = show();
    await edit("моё");
    await fireEvent.click(button("Сохранить")!);
    expect(onsave).toHaveBeenCalledWith("моё", 3);
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(readDraft(KEY)).toBe("");
  });

  it("a change under the draft hides Save and offers both choices", async () => {
    const { onsave, rerender } = show();
    await edit("моё");
    await rerender({ value: "новое", revision: 4 });
    expect(button("Сохранить")).toBeNull();
    expect(screen.getByText("новое")).toBeTruthy();
    await fireEvent.click(button("Сохранить мой текст поверх")!);
    expect(onsave).toHaveBeenCalledWith("моё", 4);
  });

  it("taking the new text rebases the draft on it", async () => {
    const { onsave, rerender } = show();
    await edit("моё");
    await rerender({ value: "новое", revision: 4 });
    await fireEvent.click(button("Взять новый текст")!);
    expect(field().value).toBe("новое");
    await fireEvent.click(button("Сохранить")!);
    expect(onsave).toHaveBeenCalledWith("новое", 4);
  });

  it("a new revision with the same text moves the base silently", async () => {
    const { onsave, rerender } = show();
    await edit("моё");
    await rerender({ value: "старое", revision: 5 });
    await fireEvent.click(button("Сохранить")!);
    expect(onsave).toHaveBeenCalledWith("моё", 5);
  });

  it("a reopened card restores the draft", () => {
    writeDraft(KEY, JSON.stringify({ text: "черновик", base: { text: "старое", revision: 3 } }));
    show();
    expect(field().value).toBe("черновик");
  });

  it("cancel drops the draft", async () => {
    show();
    await edit("моё");
    expect(readDraft(KEY)).not.toBe("");
    await fireEvent.click(button("Отмена")!);
    expect(readDraft(KEY)).toBe("");
  });
});
