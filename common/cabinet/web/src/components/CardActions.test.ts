import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import type { CardAction } from "../lib/types";
import CardActions from "./CardActions.svelte";

function action(over: Partial<CardAction>): CardAction {
  return { id: "start", label: "action.start", primary: false, agent: "", enabled: true, reason: "", params: {},
    message: "", comment: "none", brief_version: null, operations: [], ...over };
}

beforeEach(() => setLocale("ru"));
afterEach(cleanup);

describe("CardActions", () => {
  it("renders the backend's actions and none of its own", () => {
    render(CardActions, {
      actions: [action({ label: "action.start_brief", primary: true, agent: "@dev" }), action({ id: "brief", label: "action.brief" })],
      onact: vi.fn(),
    });
    const names = screen.getAllByRole("button").map((b) => b.textContent?.trim());
    expect(names).toEqual(["Одобрить ТЗ и запустить", "Разработать ТЗ"]);
    expect(screen.getByRole("button", { name: "Одобрить ТЗ и запустить" }).classList.contains("primary")).toBe(true);
  });

  it("a disabled action says why", () => {
    render(CardActions, {
      actions: [action({ id: "resume", label: "action.resume", primary: true, enabled: false, reason: "deps_open",
        params: { cards: "KD-3" } })],
      onact: vi.fn(),
    });
    expect((screen.getByRole("button", { name: "Продолжить" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("Ждёт зависимостей: KD-3")).toBeTruthy();
  });

  it("an enabled action with a reason explains itself and still runs", async () => {
    const onact = vi.fn();
    render(CardActions, { actions: [action({ reason: "brief_writing", agent: "@dev" })], onact });
    expect(screen.getByText("ТЗ ещё пишет @analyst")).toBeTruthy();
    await fireEvent.click(screen.getByRole("button", { name: "Запустить @dev" }));
    expect(onact).toHaveBeenCalledWith("start", undefined, null);
  });

  it("start sends its brief version", async () => {
    const onact = vi.fn();
    render(CardActions, { actions: [action({ label: "action.start_brief", primary: true, brief_version: 2 })], onact });
    await fireEvent.click(screen.getByRole("button", { name: "Одобрить ТЗ и запустить" }));
    expect(onact).toHaveBeenCalledWith("start", undefined, 2);
  });

  it("a rework of the brief asks what to change", async () => {
    const onact = vi.fn();
    render(CardActions, { actions: [action({ id: "brief", label: "action.brief_rework", comment: "required" })], onact });
    await fireEvent.click(screen.getByRole("button", { name: "Доработать ТЗ" }));
    await fireEvent.input(screen.getByLabelText("Что доработать в ТЗ"), { target: { value: "Добавь график" } });
    await fireEvent.click(screen.getByRole("button", { name: "Отправить" }));
    expect(onact).toHaveBeenCalledWith("brief", "Добавь график", null);
  });

  it("return asks for a comment before it sends", async () => {
    const onact = vi.fn();
    render(CardActions, {
      actions: [action({ id: "accept", label: "action.accept", primary: true }),
        action({ id: "return", label: "action.return", agent: "@dev", comment: "required" })],
      onact,
    });
    await fireEvent.click(screen.getByRole("button", { name: "Вернуть на доработку" }));
    expect(onact).not.toHaveBeenCalled();
    const send = screen.getByRole("button", { name: "Отправить" }) as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    await fireEvent.input(screen.getByLabelText("Что доработать"), { target: { value: "Нет теста" } });
    await fireEvent.click(send);
    expect(onact).toHaveBeenCalledWith("return", "Нет теста", null);
    expect(screen.queryByLabelText("Что доработать")).toBeNull();
  });

  it("reopen offers an optional comment and sends without one too", async () => {
    const onact = vi.fn();
    render(CardActions, { actions: [action({ id: "reopen", label: "action.reopen", comment: "optional" })], onact });
    await fireEvent.click(screen.getByRole("button", { name: "Переоткрыть" }));
    expect(onact).not.toHaveBeenCalled();
    const send = screen.getByRole("button", { name: "Отправить" }) as HTMLButtonElement;
    expect(send.disabled).toBe(false);
    await fireEvent.click(send);
    expect(onact).toHaveBeenCalledWith("reopen", undefined, null);
    await fireEvent.click(screen.getByRole("button", { name: "Переоткрыть" }));
    await fireEvent.input(screen.getByLabelText("Почему переоткрываете (необязательно)"), { target: { value: "Снова падает" } });
    await fireEvent.click(screen.getByRole("button", { name: "Отправить" }));
    expect(onact).toHaveBeenLastCalledWith("reopen", "Снова падает", null);
  });

  it("a start whose brief grants operations shows them before it is sent (KD-9088)", async () => {
    const onact = vi.fn();
    const step = { verb: "agent.reinstall", target: "factory", agents: ["dev"], what: "Переустановить dev",
      effect: "Новые инструкции", risk: { level: "low", why: "Очередь сохраняется" }, rollback: "Предыдущая выкатка" };
    render(CardActions, { actions: [action({ label: "action.start_brief", brief_version: 2, operations: [step] })], onact });
    await fireEvent.click(screen.getByRole("button", { name: "Одобрить ТЗ и запустить" }));
    expect(onact).not.toHaveBeenCalled();
    expect(screen.getByText("Одобрение ТЗ разрешит эти операции фабрики:")).toBeTruthy();
    expect(screen.getByText("Переустановить dev")).toBeTruthy();
    expect(screen.getByText(/Предыдущая выкатка/)).toBeTruthy();
    await fireEvent.click(screen.getByRole("button", { name: "Одобрить с операциями" }));
    expect(onact).toHaveBeenCalledWith("start", undefined, 2);
  });
});

describe("CardActions — the council (KD-9267)", () => {
  it("a start while the council has not ended says the approval cancels it, and still runs", async () => {
    const onact = vi.fn();
    render(CardActions, {
      actions: [action({ label: "action.start_brief", agent: "@dev", brief_version: 2, council: { round: 2, rounds: 3 } })],
      onact,
    });
    expect(screen.getByText("Совет не закончен: круг 2 из 3 — одобрение отменит совет")).toBeTruthy();
    await fireEvent.click(screen.getByRole("button", { name: "Одобрить ТЗ и запустить" }));
    expect(onact).toHaveBeenCalledWith("start", undefined, 2);
  });

  it("no council, no warning", () => {
    render(CardActions, { actions: [action({ label: "action.start_brief", agent: "@dev" })], onact: vi.fn() });
    expect(document.body.textContent).not.toContain("Совет");
  });
});

describe("CardActions — stages (KD-9121)", () => {
  it("a start whose brief has stages shows them before it is sent", async () => {
    const onact = vi.fn();
    const stage = { key: "a", title: "Модель", role: "@dev", spec: "implementer", depends: [], acceptance: ["Тест проходит"] };
    render(CardActions, { actions: [action({ label: "action.start_brief", brief_version: 2, stages: [stage] })], onact });
    await fireEvent.click(screen.getByRole("button", { name: "Одобрить ТЗ и запустить" }));
    expect(onact).not.toHaveBeenCalled();
    expect(screen.getByText("Одобрение ТЗ заведёт эти этапы карточками:")).toBeTruthy();
    expect(screen.getByText(/Модель/)).toBeTruthy();
    await fireEvent.click(screen.getByRole("button", { name: "Одобрить и завести этапы" }));
    expect(onact).toHaveBeenCalledWith("start", undefined, 2);
  });

  it("stages that will run as a chain say so before the approval", async () => {
    const stage = { key: "a", title: "Модель", role: "@dev", spec: "implementer", depends: [], acceptance: ["Тест проходит"] };
    const start = action({ label: "action.start_brief", brief_version: 2, stages: [stage] });
    render(CardActions, { actions: [{ ...start, chain: true }], onact: vi.fn() });
    await fireEvent.click(screen.getByRole("button", { name: "Одобрить ТЗ и запустить" }));
    expect(document.body.textContent).toContain("Этапы пойдут подряд, без вашей приёмки каждого");
    expect(document.body.textContent).toContain("Сквозной план: ветка и PR на каждый репозиторий, одна приёмка");
    cleanup();
    render(CardActions, { actions: [start], onact: vi.fn() });
    await fireEvent.click(screen.getByRole("button", { name: "Одобрить ТЗ и запустить" }));
    expect(document.body.textContent).not.toContain("Этапы пойдут подряд");
  });
});
