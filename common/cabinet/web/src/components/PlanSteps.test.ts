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
    // a typed step's command is unfolded too, labelled as a command, not as a script
    expect(items[0].querySelector("details")).toBeNull();
    expect(items[0].querySelector("pre.command")?.textContent).toBe("apt-get -q update");
    expect(items[0].querySelector(".cmd .label")?.textContent).toBe("Команда");
    expect(items[0].querySelector("pre.script")).toBeNull();
  });

  it("shows the whole text a typed step carries — a cron line or a dropped file is read, not opened", () => {
    // file.drop and cron.line carry text root runs or the kernel applies later: the owner signs exactly it
    const command =
      "python3 -I /usr/local/libexec/agent-typed-op file /etc/logrotate.d app KD-7 set '/var/log/app.log {\n daily\n}\n'";
    const drop: PlanStep = { ...typed, what: "Ротация журналов", mechanics: "node-c: /etc/logrotate.d/app", command };
    const { container } = render(PlanSteps, { steps: [drop] });
    expect(container.querySelector("details")).toBeNull();
    const pre = container.querySelector("pre.command") as HTMLPreElement;
    expect(pre.textContent).toBe(command);
  });

  it("a step without a kind is typed; one without a command shows its mechanics", () => {
    const { kind: _kind, ...legacy } = typed;
    const { container } = render(PlanSteps, { steps: [{ ...legacy, command: "" } as PlanStep] });
    expect(container.querySelector("pre.command")?.textContent).toBe("package.refresh");
    expect(container.querySelector("pre.script")).toBeNull();
  });
});
