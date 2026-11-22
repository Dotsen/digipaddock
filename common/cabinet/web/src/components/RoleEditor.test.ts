import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import RoleEditor from "./RoleEditor.svelte";

const ALL = ["cards.read", "cards.create", "decide.promote", "settings"];
const box = (name: string) => screen.getByRole("checkbox", { name }) as HTMLInputElement;

beforeEach(() => setLocale("ru"));
afterEach(cleanup);

describe("RoleEditor", () => {
  it("saves the permissions ticked, in the catalog's order", async () => {
    const onsave = vi.fn().mockResolvedValue(undefined);
    render(RoleEditor, { id: "partner", perms: ["cards.read"], all: ALL, onsave });
    const save = screen.getByRole("button", { name: "Сохранить" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    await fireEvent.click(box("decide.promote"));
    await fireEvent.click(box("cards.create"));
    expect(save.disabled).toBe(false);
    await fireEvent.click(save);
    expect(onsave).toHaveBeenCalledWith(["cards.read", "cards.create", "decide.promote"]);
  });

  it("shows «all» for the owner's role and nothing else to tick", () => {
    render(RoleEditor, { id: "owner", perms: ["*"], all: ALL, onsave: vi.fn() });
    expect(box("Все права").checked).toBe(true);
    expect(box("settings").disabled).toBe(true);
  });
});
