import { beforeEach, describe, expect, it } from "vitest";
import { setLocale } from "./i18n.svelte";
import { reasonText } from "./runReason";

beforeEach(() => setLocale("ru"));

describe("why a run or a job ended, in words", () => {
  it("names the host's and the cabinet's codes", () => {
    expect(reasonText("host_restart")).toBe("хост сессий перезапустился");
    expect(reasonText("exit 143")).toBe("процесс сессии завершился (код 143)");
  });

  it("says a session cannot sign in to Claude (KD-9214)", () => {
    expect(reasonText("auth_failed")).toBe("нет входа в Claude: токен недействителен или истёк");
  });

  it("names a worker job's reasons, not the runner's English (final review KD-9025)", () => {
    const cases: [string, string][] = [
      ["card_not_approved", "карточка не в работе для этого агента"],
      ["reassigned", "у карточки другой исполнитель"],
      ["card_done", "карточка закрыта"],
      ["card_gone", "карточки больше нет"],
      ["timeout after 3600 s: the session was killed", "превышено время (3600 с)"],
      ["the final block is over 16 KiB", "итог больше 16 КиБ"],
      ["the session ended without the specialization's final JSON block", "сессия закончилась без блока итога"],
      ["claude exited 1 without a result event", "процесс сессии завершился (код 1)"],
      ["claude reported error_during_execution", "Claude сообщил об ошибке"],
      ["auth_failed", "нет входа в Claude: токен недействителен или истёк"],
    ];
    for (const [code, text] of cases) expect(reasonText(code, code)).toBe(text);
  });

  it("keeps a person's own words", () => {
    expect(reasonText("стоп, ТЗ поменялось", "стоп, ТЗ поменялось")).toBe("стоп, ТЗ поменялось");
  });
});
