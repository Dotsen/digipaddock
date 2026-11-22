import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import StatusOverride from "./StatusOverride.svelte";

beforeEach(() => {
  setLocale("ru");
  // jsdom has no native modal implementation; browser specs check geometry and focus.
  HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  HTMLDialogElement.prototype.close = function () { this.open = false; };
});
afterEach(cleanup);

async function pick(from: string, to: string) {
  const onoverride = vi.fn();
  render(StatusOverride, { status: from, onoverride });
  await fireEvent.click(screen.getByRole("button", { name: "Сменить статус вручную" }));
  await fireEvent.click(screen.getByRole("option", { name: to }));
  return onoverride;
}

const send = () => screen.getByRole("button", { name: "Отправить" }) as HTMLButtonElement;
const reasons = () => screen.getAllByRole("radio").map((r) => r.closest("label")?.textContent?.trim());

describe("StatusOverride", () => {
  it("offers Cancelled's reasons, nothing chosen and Send off", async () => {
    const onoverride = await pick("Inbox", "Cancelled");
    expect(reasons()).toEqual(["Уже не актуально", "Дубль задачи", "Другое"]);
    expect(screen.getAllByRole("radio").some((r) => (r as HTMLInputElement).checked)).toBe(false);
    expect(send().disabled).toBe(true);
    expect(document.activeElement).toBe(screen.getAllByRole("radio")[0]);
    expect(onoverride).not.toHaveBeenCalled();
  });

  it("offers Done's reasons and sends a ready one without text", async () => {
    const onoverride = await pick("Review", "Done");
    expect(reasons()).toEqual(["Сделано вне системы", "Другое"]);
    expect(screen.getByRole("dialog", { name: "Сменить статус вручную" }).textContent).toContain("Review→Done");
    expect(screen.getByRole("button", { name: "Сменить статус вручную" }).textContent).toContain("Review");
    expect(screen.getByText(/в обход кнопок карточки/)).toBeTruthy();
    expect(send().disabled).toBe(true);
    await fireEvent.click(screen.getByRole("radio", { name: "Сделано вне системы" }));
    expect(send().disabled).toBe(false);
    await fireEvent.click(send());
    expect(onoverride).toHaveBeenCalledWith("Done", { code: "done_outside", text: "" });
  });

  it("a ready reason may carry an explanation", async () => {
    const onoverride = await pick("Review", "Done");
    await fireEvent.click(screen.getByRole("radio", { name: "Сделано вне системы" }));
    await fireEvent.input(screen.getByLabelText("Пояснение (необязательно)"), { target: { value: " в чате " } });
    await fireEvent.click(send());
    expect(onoverride).toHaveBeenCalledWith("Done", { code: "done_outside", text: "в чате" });
  });

  it("Other needs the person's own text", async () => {
    const onoverride = await pick("Review", "Done");
    await fireEvent.click(screen.getByRole("radio", { name: "Другое" }));
    expect(send().disabled).toBe(true);
    const own = screen.getByLabelText("Свой вариант причины");
    await fireEvent.input(own, { target: { value: "   " } });
    expect(send().disabled).toBe(true);
    await fireEvent.input(own, { target: { value: "сделано вне доски" } });
    await fireEvent.click(send());
    expect(onoverride).toHaveBeenCalledWith("Done", { code: "other", text: "сделано вне доски" });
  });

  it("a duplicate asks for its original's number, optional", async () => {
    const onoverride = await pick("Inbox", "Cancelled");
    expect(screen.queryByLabelText("Номер оригинала (необязательно)")).toBeNull();
    await fireEvent.click(screen.getByRole("radio", { name: "Дубль задачи" }));
    await fireEvent.input(screen.getByLabelText("Номер оригинала (необязательно)"), { target: { value: " kd-7 " } });
    await fireEvent.click(send());
    expect(onoverride).toHaveBeenCalledWith("Cancelled", { code: "duplicate", text: "", canonical: "kd-7" });
  });

  it("a duplicate without a number goes without one", async () => {
    const onoverride = await pick("Inbox", "Cancelled");
    await fireEvent.click(screen.getByRole("radio", { name: "Дубль задачи" }));
    await fireEvent.click(send());
    expect(onoverride).toHaveBeenCalledWith("Cancelled", { code: "duplicate", text: "" });
  });

  it("opens fresh after a cancel", async () => {
    const onoverride = await pick("Review", "Done");
    await fireEvent.click(screen.getByRole("radio", { name: "Сделано вне системы" }));
    await fireEvent.click(screen.getByRole("button", { name: "Отмена" }));
    expect(onoverride).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: "Сменить статус вручную" }));
    await fireEvent.click(screen.getByRole("option", { name: "Done" }));
    expect(send().disabled).toBe(true);
  });

  it("Escape dismisses the modal and restores scrolling", async () => {
    document.body.style.overflow = "auto";
    const onoverride = await pick("Review", "Done");
    expect(document.body.style.overflow).toBe("hidden");
    await fireEvent(screen.getByRole("dialog"), new Event("cancel"));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.body.style.overflow).toBe("auto");
    expect(onoverride).not.toHaveBeenCalled();
    document.body.style.overflow = "";
  });
});
