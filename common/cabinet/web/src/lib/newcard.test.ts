// the new card's form — the body it sends and the project it remembers per person.
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearDrafts } from "./drafts";
import { emptyDraft, hasMore, keepDraft, keptDraft, lastProject, newCardBody, rememberProject, type NewCardDraft } from "./newcard";

const draft = (over: Partial<NewCardDraft> = {}): NewCardDraft => ({ ...emptyDraft("shop"), title: "Форма", ...over });

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
  clearDrafts();
});

describe("the unsent draft", () => {
  it("is kept per person while it has text, and goes with a sign-out", () => {
    const mine = draft({ description: "Текст", labels: ["ui"], dependencies: ["KD-4"] });
    keepDraft("owner", mine);
    expect(keptDraft("owner")).toEqual(mine);
    expect(keptDraft("anna")).toBeNull();
    clearDrafts();
    expect(keptDraft("owner")).toBeNull();
  });

  it("a draft without text is not kept: «Ещё» alone is not carried to another day", () => {
    keepDraft("owner", draft({ description: "Текст" }));
    keepDraft("owner", draft({ title: " ", labels: ["ui"], parent: "KD-3" }));
    expect(keptDraft("owner")).toBeNull();
  });

  // a tab opened before the release holds the labels as typed text
  it("a draft kept in the old shape (the labels as text) is not a draft", () => {
    sessionStorage.setItem("kd.draft.owner.new.card", JSON.stringify({ ...draft({ description: "Текст" }), labels: "ui, night" }));
    expect(keptDraft("owner")).toBeNull();
  });

  // milestones are gone; a tab opened before the release kept one with its draft
  it("a draft kept with a milestone restores without it", () => {
    const text = draft({ description: "Текст" });
    sessionStorage.setItem("kd.draft.owner.new.card", JSON.stringify({ ...text, milestone: "m-2" }));
    expect(keptDraft("owner")).toEqual(text);
    expect(newCardBody(keptDraft("owner")!)).toEqual({ title: "Форма", project: "shop", description: "Текст" });
  });

  it("«Ещё» holds something once a label, a link or a setting is chosen", () => {
    expect(hasMore(draft())).toBe(false);
    expect([{ labels: ["ui"] }, { parent: "KD-3" }, { dependencies: ["KD-4"] }, { assignee: "" }].map((over) => hasMore(draft(over)))).toEqual([
      true, true, true, true,
    ]);
  });

  it("what is stored in another shape is not a draft", () => {
    sessionStorage.setItem("kd.draft.owner.new.card", JSON.stringify({ title: "x", project: 5 }));
    expect(keptDraft("owner")).toBeNull();
    sessionStorage.setItem("kd.draft.owner.new.card", "{");
    expect(keptDraft("owner")).toBeNull();
  });
});

describe("newCardBody", () => {
  it("an untouched form sends the title and the project", () => {
    expect(newCardBody(draft({ title: "  Форма  " }))).toEqual({ title: "Форма", project: "shop" });
  });

  it("a filled form sends every field as the API holds it", () => {
    const council = { brief: { critics: ["claude:opus"], rounds: 2 } };
    expect(
      newCardBody(
        draft({
          description: "Зачем\n",
          assignee: "@dev",
          labels: ["ui", " ", " night ", "ui"],
          acceptance: [" A ", "", "B"],
          parent: " KD-3 ",
          dependencies: ["KD-5", "KD-4", "KD-5"],
          model: "claude:opus",
          effort: "high",
          council,
        }),
      ),
    ).toEqual({
      title: "Форма",
      project: "shop",
      description: "Зачем\n",
      assignee: "@dev",
      labels: ["ui", "night"],
      acceptance: [
        { text: "A", checked: false },
        { text: "B", checked: false },
      ],
      parent: "KD-3",
      dependencies: ["KD-5", "KD-4"],
      model: "claude:opus",
      effort: "high",
      council,
    });
  });

  it("«nobody» is an assignee of its own; the project's route is not sent", () => {
    expect(newCardBody(draft({ assignee: "" }))).toEqual({ title: "Форма", project: "shop", assignee: "" });
    expect(newCardBody(draft({ assignee: null }))).not.toHaveProperty("assignee");
  });

  it("a kind «none at all» is a council of its own", () => {
    const council = { brief: { critics: [], rounds: 3 } };
    expect(newCardBody(draft({ council }))).toEqual({ title: "Форма", project: "shop", council });
  });
});

describe("the project last used", () => {
  it("is remembered per person", () => {
    rememberProject("owner", "shop");
    rememberProject("anna", "web");
    expect([lastProject("owner"), lastProject("anna"), lastProject("ivan")]).toEqual(["shop", "web", ""]);
  });

  it("a closed storage remembers nothing and breaks nothing", () => {
    const closed = () => {
      throw new Error("denied");
    };
    vi.stubGlobal("localStorage", { getItem: closed, setItem: closed });
    expect(() => rememberProject("owner", "shop")).not.toThrow();
    expect(lastProject("owner")).toBe("");
  });
});
