import { cleanup, render } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import type { PlanStep } from "../lib/types";
import PlanSteps from "./PlanSteps.svelte";

const SCRIPT = "set -e\nnginx -t\nsystemctl reload nginx.service\n";

const typed: PlanStep = {
  n: 1,
  what: "Обновить индексы пакетов",
  mechanics: "package.refresh",
  effect: "",
  risk: { level: "low", why: "" },
  rollback: "",
  command: "apt-get -q update",
  servers: ["node-c"],
  kind: "typed",
};
const script: PlanStep = { ...typed, n: 2, what: "Перечитать конфигурацию nginx", mechanics: "node-c: скрипт, 3 строк", command: SCRIPT, kind: "script" };

beforeEach(() => setLocale("ru"));
afterEach(cleanup);

describe("PlanSteps", () => {
  it("shows a script step unfolded — the owner signs exactly this text", () => {
    const { container } = render(PlanSteps, { steps: [typed, script] });
    const items = container.querySelectorAll(".steps > li");
    expect(items).toHaveLength(2);
    // the script is a visible <pre>, labelled, with no <details> to open
    const pre = items[1].querySelector("pre.script") as HTMLPreElement;
    expect(pre.textContent).toBe(SCRIPT);
    expect(items[1].querySelector("details")).toBeNull();
    expect(items[1].textContent).toContain("Скрипт — выполняется ровно этот текст");
    // a typed step keeps its command folded
    const details = items[0].querySelector("details.cmd") as HTMLDetailsElement;
    expect(details.open).toBe(false);
    expect(details.querySelector("pre")?.textContent).toBe("apt-get -q update");
    expect(items[0].querySelector("pre.script")).toBeNull();
  });

  it("a step without a kind is typed", () => {
    const { kind: _kind, ...legacy } = typed;
    const { container } = render(PlanSteps, { steps: [legacy as PlanStep] });
    expect(container.querySelector("details.cmd")).not.toBeNull();
    expect(container.querySelector("pre.script")).toBeNull();
  });
});
