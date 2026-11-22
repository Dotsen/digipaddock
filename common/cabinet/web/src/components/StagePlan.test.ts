import { cleanup, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import type { Stage } from "../lib/types";
import StagePlan from "./StagePlan.svelte";

const stages: Stage[] = [
  { key: "a", title: "Модель", role: "@dev", spec: "implementer", depends: [], description: "Схема", acceptance: ["Тест проходит"] },
  { key: "b", title: "Веб", role: "@dev", spec: "docs", depends: ["a"], acceptance: ["Vitest проходит"], child: "KD-12", status: "Blocked" },
];

beforeEach(() => setLocale("ru"));
afterEach(cleanup);

describe("StagePlan", () => {
  it("lists each stage with its role, what it waits for and its card", () => {
    const { container } = render(StagePlan, { stages });
    const items = container.querySelectorAll(".stages > li");
    expect(items).toHaveLength(2);
    expect(items[0].textContent).toContain("Модель");
    expect(items[0].textContent).toContain("@dev · implementer");
    expect(items[0].textContent).toContain("Тест проходит");
    expect(items[1].textContent).toContain("после: a");
    const link = screen.getByRole("link", { name: /KD-12/ }) as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe("/cards/KD-12");
    expect(items[1].textContent).toContain("Blocked");
  });

  it("a chain plan says so and words where each stage stands at its gate", () => {
    const stage = (key: string, status: string, gate: string, reason = ""): Stage =>
      ({ key, title: key, role: "@dev", depends: [], child: `KD-1${key}`, status, gate, reason });
    const chained = [
      stage("a", "Done", "passed"), stage("b", "Review", "reviewing"), stage("c", "Review", "ready"),
      stage("d", "Review", "fixes"), stage("e", "In Progress", "returned"), stage("f", "Review", "skipped"),
      stage("g", "Review", "held", "расходится с ТЗ"), stage("h", "Review", "held", "boss_silent"),
      stage("i", "Review", "held", "boss_absent"), stage("j", "Review", "held", "review_failed: rate limit"),
      stage("k", "Blocked", ""),
    ];
    const { container } = render(StagePlan, { stages: chained, chain: true, compact: true });
    expect(container.textContent).toContain("Сквозной план: ветка и PR на каждый репозиторий, одна приёмка");
    const text = [...container.querySelectorAll(".stages > li")].map((li) => li.querySelector(".gate")?.textContent ?? "");
    expect(text).toEqual([
      "Пропущен BOSS, ждёт общей приёмки", "Сдан, идёт проверка кода", "Проверен, решает BOSS",
      "Проверка нашла замечания, решает BOSS", "Возвращён по проверке кода", "Сдан без нового кода, решает BOSS",
      "Ждёт вас: расходится с ТЗ", "Ждёт вас: BOSS не ответил за 30 минут", "Ждёт вас: BOSS недоступен",
      "Ждёт вас: проверка кода не состоялась (rate limit)", "",
    ]);
  });

  it("a plain plan is drawn as before", () => {
    const { container } = render(StagePlan, { stages });
    expect(container.textContent).not.toContain("Сквозной план");
    expect(container.querySelector(".gate")).toBeNull();
  });

  it("compact leaves the texts out", () => {
    const { container } = render(StagePlan, { stages, compact: true });
    expect(container.textContent).not.toContain("Тест проходит");
    expect(container.textContent).toContain("Модель");
  });
});
