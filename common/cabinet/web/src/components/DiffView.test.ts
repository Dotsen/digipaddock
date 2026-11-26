import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import DiffView from "./DiffView.svelte";

const TEXT = [
  "diff --git a/common/kd/x.py b/common/kd/x.py",
  "--- a/common/kd/x.py",
  "+++ b/common/kd/x.py",
  "@@ -1,2 +1,2 @@",
  "-b = 2",
  "+b = 3",
  " d = 5",
].join("\n");

beforeEach(() => setLocale("ru"));
afterEach(() => cleanup());

describe("DiffView", () => {
  it("shows the lines; without commenting there is no comment button", () => {
    render(DiffView, { text: TEXT, truncated: true, url: "https://forge/pr/12", commenting: false, comments: [] });
    expect(screen.getByText("common/kd/x.py")).toBeTruthy();
    expect(screen.getByText("b = 3")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Замечание к строке/ })).toBeNull();
    // a cut diff says so and links the whole one
    expect(screen.getByRole("link", { name: /полностью в Forgejo/ }).getAttribute("href")).toBe("https://forge/pr/12");
  });

  it("DiffView adds a line comment", async () => {
    render(DiffView, { text: TEXT, truncated: false, url: "", commenting: true, comments: [] });
    // an added line is commented on the new side, a removed one on the old side
    await fireEvent.click(screen.getByRole("button", { name: "Замечание к строке 2 (новая)" }));
    await fireEvent.input(screen.getByRole("textbox", { name: "Замечание к строке" }), { target: { value: "зачем 3?" } });
    await fireEvent.click(screen.getByRole("button", { name: "Добавить" }));
    expect(screen.getByText("зачем 3?")).toBeTruthy();
    expect(screen.queryByRole("textbox")).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: "Замечание к строке 1 (старая)" }));
    expect(screen.getByRole("button", { name: "Добавить" }).hasAttribute("disabled")).toBe(true);
    await fireEvent.click(screen.getByRole("button", { name: "Отмена" }));
    await fireEvent.click(screen.getByRole("button", { name: "Удалить замечание" }));
    expect(screen.queryByText("зачем 3?")).toBeNull();
  });

  it("DiffView takes at most 30 line remarks, as Forgejo's review does", () => {
    const comments = Array.from({ length: 30 }, (_, i) => ({ path: "other.py", line: i + 1, side: "new" as const, body: "x" }));
    render(DiffView, { text: TEXT, truncated: false, url: "", commenting: true, comments });
    const plus = screen.getAllByRole("button", { name: /Замечание к строке/ });
    expect(plus.length).toBe(3);
    expect(plus.every((b) => b.hasAttribute("disabled"))).toBe(true);
  });

  it("warns about hidden characters and shows their code points in the line", () => {
    const text = TEXT.replace("+b = 3", "+b = 3 // \u202E");
    render(DiffView, { text, truncated: false, url: "", commenting: false, comments: [] });
    expect(screen.getByRole("alert").textContent).toContain("U+202E");
    expect(screen.getByText("U+202E", { selector: "mark" })).toBeTruthy();
    cleanup();
    render(DiffView, { text: TEXT, truncated: false, url: "", commenting: false, comments: [] });
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
