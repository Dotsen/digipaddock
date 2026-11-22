import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import ChipMenu from "./ChipMenu.svelte";

const options = [
  { value: "Inbox", text: "Inbox" },
  { value: "To Do", text: "To Do", tone: "info" },
  { value: "Done", text: "Done", tone: "good" },
];

afterEach(cleanup);

describe("ChipMenu", () => {
  // in a modal sheet (the new card's form) Escape is also the sheet's «close»: an open menu takes it
  it("Escape closes the open menu and is not passed on; with the menu closed it is the page's", async () => {
    render(ChipMenu, { value: "Inbox", options, label: "Сменить статус", onchange: vi.fn() });
    const chip = screen.getByRole("button", { name: "Сменить статус" });
    const outer = vi.fn();
    document.body.addEventListener("keydown", outer);
    await fireEvent.click(chip);
    expect(await fireEvent.keyDown(chip, { key: "Escape" })).toBe(false); // preventDefault: no close request
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(outer).not.toHaveBeenCalled();
    expect(await fireEvent.keyDown(chip, { key: "Escape" })).toBe(true);
    expect(outer).toHaveBeenCalledTimes(1);
    document.body.removeEventListener("keydown", outer);
  });

  it("shows the value as a chip and the choices only after a click", async () => {
    const onchange = vi.fn();
    render(ChipMenu, { value: "Inbox", options, label: "Сменить статус", onchange });
    const chip = screen.getByRole("button", { name: "Сменить статус" });
    expect(chip.textContent).toContain("Inbox");
    expect(screen.queryByRole("listbox")).toBeNull();
    await fireEvent.click(chip);
    expect(screen.getAllByRole("option").map((o) => o.textContent?.trim())).toEqual(["Inbox", "To Do", "Done"]);
    expect(screen.getByRole("option", { name: "Inbox" }).getAttribute("aria-selected")).toBe("true");
    await fireEvent.click(screen.getByRole("option", { name: "Done" }));
    expect(onchange).toHaveBeenCalledWith("Done");
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("closes without a change on the same value or Escape", async () => {
    const onchange = vi.fn();
    render(ChipMenu, { value: "Inbox", options, label: "Сменить статус", onchange });
    await fireEvent.click(screen.getByRole("button", { name: "Сменить статус" }));
    await fireEvent.click(screen.getByRole("option", { name: "Inbox" }));
    expect(onchange).not.toHaveBeenCalled();
    expect(screen.queryByRole("listbox")).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: "Сменить статус" }));
    await fireEvent.keyDown(screen.getByRole("listbox"), { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
  });
});
