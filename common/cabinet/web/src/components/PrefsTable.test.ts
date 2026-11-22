import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import PrefsTable from "./PrefsTable.svelte";

beforeEach(() => setLocale("ru"));
afterEach(cleanup);

const kinds = { "decision.waiting": "now", "card.done": "batch", progress: "feed" } as const;
const radio = (name: string) => screen.getByRole("radio", { name }) as HTMLInputElement;

describe("PrefsTable", () => {
  it("shows one row per kind with its mode chosen", () => {
    render(PrefsTable, { kinds: { ...kinds }, onchange: () => {} });
    expect(screen.getAllByRole("row")).toHaveLength(4); // the head and three kinds
    expect(radio("Решение ждёт меня: Сразу").checked).toBe(true);
    expect(radio("Карточка готова: Пакетом").checked).toBe(true);
    expect(radio("Ход работы агентов: Только в ленту").checked).toBe(true);
    expect(radio("Карточка готова: Сразу").checked).toBe(false);
  });

  it("reports the new mode of a kind", async () => {
    const onchange = vi.fn();
    render(PrefsTable, { kinds: { ...kinds }, onchange });
    await fireEvent.click(radio("Карточка готова: Сразу"));
    expect(onchange).toHaveBeenCalledWith("card.done", "now");
  });

  it("is read-only while saving", () => {
    render(PrefsTable, { kinds: { ...kinds }, onchange: () => {}, disabled: true });
    for (const each of screen.getAllByRole("radio")) expect((each as HTMLInputElement).disabled).toBe(true);
  });
});
