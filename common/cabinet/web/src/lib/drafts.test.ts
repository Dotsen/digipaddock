import { afterEach, describe, expect, it } from "vitest";
import { afterSend, clearDrafts, draftKey, isSendKey, readDraft, writeDraft } from "./drafts";

afterEach(() => sessionStorage.clear());

const key = (init: KeyboardEventInit) => new KeyboardEvent("keydown", init);

describe("drafts", () => {
  it("names a draft by person, card and context", () => {
    expect(draftKey("owner", "KD-5", "thread")).toBe("kd.draft.owner.KD-5.thread");
  });

  it("keeps what was typed after the sent text", () => {
    expect(afterSend("ответ\nещё", "ответ")).toBe("ещё");
    expect(afterSend("ответ", "ответ")).toBe("");
    expect(afterSend("другой", "ответ")).toBe("другой");
  });

  it("stores a draft and drops it when it is empty", () => {
    const k = draftKey("owner", "KD-5", "thread");
    writeDraft(k, "черновик");
    expect(readDraft(k)).toBe("черновик");
    writeDraft(k, "");
    expect(sessionStorage.getItem(k)).toBeNull();
    expect(readDraft(k)).toBe("");
  });

  it("clears only the drafts", () => {
    writeDraft(draftKey("owner", "KD-5", "thread"), "a");
    writeDraft(draftKey("anna", "KD-6", "reply:d1"), "b");
    sessionStorage.setItem("other", "x");
    clearDrafts();
    expect(sessionStorage.length).toBe(1);
    expect(sessionStorage.getItem("other")).toBe("x");
  });

  it("sends on Ctrl+Enter or Cmd+Enter only", () => {
    expect(isSendKey(key({ key: "Enter" }))).toBe(false);
    expect(isSendKey(key({ key: "Enter", shiftKey: true }))).toBe(false);
    expect(isSendKey(key({ key: "Enter", ctrlKey: true }))).toBe(true);
    expect(isSendKey(key({ key: "Enter", metaKey: true }))).toBe(true);
    expect(isSendKey(key({ key: "Enter", ctrlKey: true, isComposing: true }))).toBe(false);
  });
});
