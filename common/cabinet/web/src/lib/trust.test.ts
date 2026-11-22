import { beforeEach, describe, expect, it } from "vitest";
import { setLocale } from "./i18n.svelte";
import { applyLine, describeTrust, proposeMany, trustTitle, trustWhat } from "./trust";

describe("the monitor's trust in words (KD-9056)", () => {
  beforeEach(() => setLocale("ru"));

  it("names what becomes trusted by its kind", () => {
    expect(trustTitle({ kind: "accept", incident: 7, check: "logins", subject: "root@key" })).toBe(
      "Принять в эталон: P-7 · logins root@key",
    );
    expect(trustTitle({ kind: "baseline", server: "all" })).toBe("Снять эталон: всех серверов");
    expect(trustTitle({ kind: "trust", cidr: "10.1.2.3", label: "офис" })).toBe("Доверенный адрес: 10.1.2.3 · офис");
    expect(trustWhat({ kind: "accept", incident: { id: "P-9" } })).toBe("P-9 в эталон");
    expect(trustTitle({ kind: "revoke", incident: 603, check: "logins", subject: "promo@key" })).toBe(
      "Доступ снят: P-603 · logins promo@key",
    );
    expect(trustWhat({ kind: "revoke", incident: { id: "P-603" } })).toBe("P-603: доступ снят");
  });

  it("says how the monitor applied it", () => {
    expect(applyLine("pending")).toBe("Подписано — ждёт применения монитором");
    expect(applyLine("error", "P-7 закрыта")).toBe("Ошибка применения: P-7 закрыта");
    expect(applyLine("")).toBe("");
  });

  it("describes the journal's steps", () => {
    expect(describeTrust("monitor_trust.failed", { kind: "baseline", server: "node-c", detail: "сервер недоступен" })).toBe(
      "Монитор не применил: эталон node-c — сервер недоступен",
    );
    expect(describeTrust("monitor_trust.removed", { cidr: "10.1.2.3" })).toBe("Убран из доверенных: 10.1.2.3");
  });
});

describe("a batch accept (KD-9196)", () => {
  it("proposes the P-n as one bundle and reads each decision for the signing dialog", async () => {
    const posted: unknown[] = [];
    const post = async (path: string, body: unknown) => {
      posted.push([path, body]);
      return { decisions: [{ id: "d1" }, { id: "d2" }], refused: [{ incident: "P-3", error: "P-3 закрыта" }] };
    };
    const get = async (path: string) => ({ decision: { id: path.split("/").pop() }, view: {} });
    const got = await proposeMany(["P-1", "P-2", "P-3"], post as any, get as any);
    expect(posted).toEqual([["/api/monitor-trust/batch", { incidents: ["P-1", "P-2", "P-3"] }]]);
    expect(got.items.map((i) => i.decision.id)).toEqual(["d1", "d2"]);
    expect(got.refused).toEqual([{ incident: "P-3", error: "P-3 закрыта" }]);
  });

  it("proposes «доступ снят» for many P-n with its kind (KD-9272)", async () => {
    const posted: unknown[] = [];
    const post = async (path: string, body: unknown) => (posted.push([path, body]), { decisions: [], refused: [] });
    await proposeMany(["P-1"], post as any, (async () => ({})) as any, "revoke");
    expect(posted).toEqual([["/api/monitor-trust/batch", { incidents: ["P-1"], kind: "revoke" }]]);
  });
});
