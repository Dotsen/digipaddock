import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import { FORM, LONG } from "../lib/questions.samples";
import QuestionForm from "./QuestionForm.svelte";

const KEY = "kd.draft.owner.KD-5.form:d1:sha";

function show(over: Record<string, unknown> = {}) {
  const onsubmit = vi.fn(async () => true);
  const props = { form: FORM, origin: "agent" as "agent" | "parsed", original: "", draftKey: KEY, busy: false, onsubmit, ...over };
  render(QuestionForm, props);
  return props as typeof props & { onsubmit: typeof onsubmit };
}

const radio = (name: RegExp) => screen.getByRole("radio", { name }) as HTMLInputElement;
const box = (name: RegExp) => screen.getByRole("checkbox", { name }) as HTMLInputElement;
const field = (name: string | RegExp) => screen.getByRole("textbox", { name }) as HTMLTextAreaElement;
const submit = () => screen.getByRole("button", { name: "Отправить ответы" }) as HTMLButtonElement;

beforeEach(() => setLocale("ru"));
afterEach(() => {
  cleanup();
  sessionStorage.clear();
});

describe("QuestionForm", () => {
  it("marks the recommended option and never picks it", () => {
    show();
    expect(radio(/HTTP/).checked).toBe(false);
    expect(radio(/HTTP/).closest("label")?.textContent).toContain("Рекомендуется");
    expect(radio(/SSH/).closest("label")?.textContent).not.toContain("Рекомендуется");
  });

  it("a short form is one list", () => {
    show();
    expect(screen.getByText("Как проверять?")).toBeTruthy();
    expect(screen.getByText("Что ещё?")).toBeTruthy();
    expect(screen.getByText("Условия")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Далее" })).toBeNull();
  });

  it("a long form steps with N of M, back, next and a review", async () => {
    show({ form: LONG });
    expect(screen.getByText("1 из 4")).toBeTruthy();
    expect(screen.queryByText("Что ещё?")).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: "Далее" }));
    expect(screen.getByText("2 из 4")).toBeTruthy();
    await fireEvent.click(screen.getByRole("button", { name: "Назад" }));
    expect(screen.getByText("1 из 4")).toBeTruthy();
    await fireEvent.click(radio(/SSH/));
    for (let i = 0; i < 4; i += 1) await fireEvent.click(screen.getByRole("button", { name: /Далее|Проверить ответы/ }));
    expect(screen.getAllByRole("button", { name: "Изменить" })).toHaveLength(4);
    expect(screen.getByText(/SSH/)).toBeTruthy();
    await fireEvent.click(screen.getAllByRole("button", { name: "Изменить" })[2]);
    expect(screen.getByText("3 из 4")).toBeTruthy();
  });

  it("submit waits for the required answers", async () => {
    show();
    expect(submit().disabled).toBe(true);
    expect(screen.getByText("Ответьте на обязательные: 1, 3")).toBeTruthy();
    await fireEvent.click(radio(/SSH/));
    await fireEvent.input(field("Условия"), { target: { value: "до пятницы" } });
    expect(submit().disabled).toBe(false);
    expect(screen.queryByText(/Ответьте на обязательные/)).toBeNull();
  });

  it("a single choice holds one option, a multiple choice many", async () => {
    show();
    await fireEvent.click(radio(/HTTP/));
    await fireEvent.click(radio(/SSH/));
    expect([radio(/HTTP/).checked, radio(/SSH/).checked]).toEqual([false, true]);
    await fireEvent.click(box(/Обработчик/));
    await fireEvent.click(box(/Очередь/));
    expect([box(/Обработчик/).checked, box(/Очередь/).checked]).toEqual([true, true]);
  });

  it("Enter in a field is a new line, Ctrl+Enter submits", async () => {
    const props = show();
    await fireEvent.click(radio(/SSH/));
    const details = field("Условия");
    await fireEvent.input(details, { target: { value: "строка 1\nстрока 2" } });
    await fireEvent.keyDown(details, { key: "Enter" });
    expect(props.onsubmit).not.toHaveBeenCalled();
    await fireEvent.keyDown(details, { key: "Enter", ctrlKey: true });
    expect(props.onsubmit).toHaveBeenCalledWith({
      answers: {
        method: { selected_option_ids: ["ssh"], custom_text: "" },
        extra: { selected_option_ids: [], custom_text: "" },
        details: { selected_option_ids: [], custom_text: "строка 1\nстрока 2" },
      },
      comment: "",
    });
  });

  it("in a long form Ctrl+Enter is the next step, the review, then the submit", async () => {
    const props = show({ form: LONG });
    const ctrlEnter = (el: HTMLElement) => fireEvent.keyDown(el, { key: "Enter", ctrlKey: true });
    await fireEvent.click(radio(/SSH/));
    await ctrlEnter(radio(/SSH/));
    expect(screen.getByText("2 из 4")).toBeTruthy();
    await ctrlEnter(field("Что ещё? — Свой ответ"));
    expect(screen.getByText("3 из 4")).toBeTruthy();
    await fireEvent.input(field("Условия"), { target: { value: "ночью" } });
    await ctrlEnter(field("Условия"));
    expect(screen.getByText("4 из 4")).toBeTruthy();
    expect(props.onsubmit).not.toHaveBeenCalled();
    await ctrlEnter(radio(/^S/));
    expect(screen.getAllByRole("button", { name: "Изменить" })).toHaveLength(4);
    expect(props.onsubmit).not.toHaveBeenCalled();
    await ctrlEnter(field("Общее пояснение (необязательно)"));
    expect(props.onsubmit).toHaveBeenCalledTimes(1);
  });

  it("the draft keeps the choices across a remount and goes after a sent answer", async () => {
    show();
    await fireEvent.click(radio(/SSH/));
    await fireEvent.input(field("Условия"), { target: { value: "до пятницы" } });
    await fireEvent.input(field("Общее пояснение (необязательно)"), { target: { value: "спасибо" } });
    cleanup();
    const again = show();
    expect(radio(/SSH/).checked).toBe(true);
    expect(field("Условия").value).toBe("до пятницы");
    expect(field("Общее пояснение (необязательно)").value).toBe("спасибо");
    await fireEvent.click(submit());
    await vi.waitFor(() => expect(sessionStorage.getItem(KEY)).toBeNull());
    expect(again.onsubmit).toHaveBeenCalledOnce();
  });

  it("a failed submit keeps the choices and the draft", async () => {
    const props = show({ onsubmit: vi.fn(async () => false) });
    await fireEvent.click(radio(/SSH/));
    await fireEvent.input(field("Условия"), { target: { value: "до пятницы" } });
    await fireEvent.click(submit());
    await vi.waitFor(() => expect(props.onsubmit).toHaveBeenCalled());
    expect(radio(/SSH/).checked).toBe(true);
    expect(sessionStorage.getItem(KEY)).toContain("ssh");
  });

  it("a recognized form says so, shows the original and offers free text", async () => {
    const onfree = vi.fn(async () => true);
    show({ origin: "parsed", original: "1. Как проверять? А) HTTP; Б) SSH.", onfree });
    expect(screen.getByText("Варианты распознаны из текста")).toBeTruthy();
    expect(screen.getByText("Исходный текст вопроса")).toBeTruthy();
    await fireEvent.click(screen.getByRole("button", { name: "Ответить свободным текстом" }));
    expect(screen.queryByRole("radio")).toBeNull();
    const free = screen.getByRole("textbox", { name: "Ваш ответ…" });
    await fireEvent.input(free, { target: { value: "А, остальное — как считаешь" } });
    await fireEvent.keyDown(free, { key: "Enter", ctrlKey: true });
    expect(onfree).toHaveBeenCalledWith("А, остальное — как считаешь");
    await fireEvent.click(screen.getByRole("button", { name: "Ответить по пунктам" }));
    expect(radio(/SSH/)).toBeTruthy();
  });
});
