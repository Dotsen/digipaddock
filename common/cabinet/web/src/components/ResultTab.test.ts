import { cleanup, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import type { CardResult, ReviewIteration } from "../lib/types";
import ResultTab from "./ResultTab.svelte";

const live = vi.hoisted(() => ({ listener: null as ((e: unknown) => void) | null }));
vi.mock("../lib/live.svelte", async (original) => ({
  ...(await original<object>()),
  onLiveEvent: (listener: (e: unknown) => void) => {
    live.listener = listener;
    return () => (live.listener = null);
  },
}));

function result(over: Partial<CardResult> = {}): CardResult {
  return { summary: "", plan: "", notes: "", acceptance: [], dod: "", links: [], reviews: [], ...over };
}

function review(over: Partial<ReviewIteration>): ReviewIteration {
  return {
    iteration: 1, entered: "2026-10-17T10:00:00Z", by: "agent:dev", by_name: "@dev", entered_note: "",
    outcome: "pending", decided: "", decided_by: "", decided_by_name: "", note: "", ...over,
  };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

beforeEach(() => setLocale("ru"));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ResultTab", () => {
  it("an empty result says there is no summary yet", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(result())));
    render(ResultTab, { cardId: "KD-5" });
    await screen.findByText("Итога пока нет");
    expect(screen.queryByText("План")).toBeNull();
  });

  it("shows the summary, links, AC and DoD counts, plan and notes, and the reviews", async () => {
    const found = result({
      summary: "Сделано: **кеш**",
      links: ["https://forge.example/pr/1"],
      acceptance: [{ text: "Есть кеш", checked: true }, { text: "Есть тест", checked: false }],
      dod: "- [x] #1 Ревью\n- [ ] #2 Выкатка",
      plan: "1. Кеш",
      notes: "Заметка",
      reviews: [
        review({ outcome: "returned", decided: "2026-10-17T11:00:00Z", decided_by: "user:owner", decided_by_name: "Владелец", note: "Нет теста" }),
        review({ iteration: 2, entered_note: "Тест добавлен" }),
      ],
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(found)));
    render(ResultTab, { cardId: "KD-5" });
    await screen.findByText("кеш");
    expect(screen.getByRole("link", { name: "https://forge.example/pr/1" })).toBeTruthy();
    expect(document.body.textContent).toContain("выполнено 1 из 2");
    expect(document.body.textContent).toContain("Ревью");
    expect(screen.getByText("План")).toBeTruthy();
    expect(screen.getByText("Заметки")).toBeTruthy();
    const text = document.body.textContent ?? "";
    expect(text).toContain("Проверка 1");
    expect(text).toContain("возвращено: Владелец");
    expect(text).toContain("Нет теста");
    expect(text).toContain("Проверка 2");
    expect(text).toContain("ждёт проверки");
    expect(text).toContain("Тест добавлен");
    expect(screen.queryAllByRole("checkbox")).toEqual([]);
  });

  it("reads the result again when the card changes", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json(result()))
      .mockResolvedValueOnce(json(result({ summary: "Готово" })));
    vi.stubGlobal("fetch", fetch);
    render(ResultTab, { cardId: "KD-5" });
    await screen.findByText("Итога пока нет");
    live.listener?.({ seq: 3, kind: "card.changed", card: "KD-6", payload: {} });
    live.listener?.({ seq: 4, kind: "card.changed", card: "KD-5", payload: {} });
    await screen.findByText("Готово");
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("a failed read says so with a retry", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json({}, 403)));
    render(ResultTab, { cardId: "KD-5" });
    await screen.findByText("Не удалось загрузить результат");
    expect(screen.getByRole("button", { name: "Повторить" })).toBeTruthy();
  });
});

describe("ResultTab — the release (KD-9121)", () => {
  it("names the release state with its revision and why", async () => {
    const found = result({ release: { state: "degraded", rev: "abc1234", at: "2026-10-17T10:00:00Z", why: "cabinet_schema: schema 28 served", host: "stage", operations: [], seq: 7, key: "notice.release.degraded" } });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(found)));
    render(ResultTab, { cardId: "KD-5" });
    await screen.findByText("Выкатка");
    const text = document.body.textContent ?? "";
    expect(text).toContain("не прошла проверку");
    expect(text).toContain("abc1234");
    expect(text).toContain("cabinet_schema: schema 28 served");
  });
});
