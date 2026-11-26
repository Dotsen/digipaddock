import { beforeEach, describe, expect, it } from "vitest";
import { actorName, describeEvent, incidentOf, isEcho } from "./events";
import { setLocale } from "./i18n.svelte";

const event = (kind: string, payload: Record<string, any> = {}, actor = "agent:dev") => ({
  seq: 1,
  ts: "2026-10-15T09:00:00Z",
  actor,
  kind,
  card: "KD-5",
  payload,
});

beforeEach(() => setLocale("ru"));

describe("journal events in words", () => {
  it("names a new card and a status move", () => {
    expect(describeEvent(event("card.created", { title: "Кеш" }))).toBe("Создал карточку «Кеш»");
    const moved = { fields: ["status"], before: { status: "To Do" }, after: { status: "Review" } };
    expect(describeEvent(event("card.changed", moved))).toBe("To Do → Review");
    expect(describeEvent(event("card.changed", { fields: ["labels"], before: {}, after: {} }))).toBe("Изменено: labels");
  });

  it("names a project's change by the project's name (KD-9064)", () => {
    expect(describeEvent(event("project.created", { project: "sayt", name: "Сайт", route: "@dev" }, "user:owner"))).toBe(
      "Создан проект Сайт",
    );
    // the list is not read here: the id stands in for the name
    expect(describeEvent(event("project.deleted", { project: "ops", target: "digipaddock", cards: 2 }, "user:owner"))).toBe(
      "Удалён проект ops, карточки перенесены в digipaddock",
    );
    // a repository request, its outcome, an unbind
    expect(describeEvent(event("project.repo", { project: "web", repo: "o/Site", state: "no_access" }, "root"))).toBe(
      "Репозиторий проекта web (o/Site): нет доступа у dev",
    );
    expect(describeEvent(event("project.repo", { project: "web", repo: "", state: "" }, "root"))).toBe(
      "Репозиторий отвязан от проекта web",
    );
  });

  it("names a role's context limit change with the old and the new value (KD-9462)", () => {
    const changed = (from: unknown, to: unknown) =>
      describeEvent(event("context_limit.changed", { agent: "dev", limit: { from, to } }, "user:owner")).replace(/\s/g, " ");
    expect(changed(400000, 250000)).toBe("Предел контекста @dev: 400 000 → 250 000");
    expect(changed(250000, "none")).toBe("Предел контекста @dev: 250 000 → без предела");
  });

  it("says who decided what", () => {
    const text = describeEvent(event("decision.approved", { type: "admin_plan" }, "user:owner"));
    expect(text).toContain("@owner");
  });

  it("renders the digest from its key and facts", () => {
    const digest = event("digest", { user: "owner", key: "push.digest.body", params: { decisions: 2, incidents: 0, done: 3, cost: "12 k" } }, "system");
    const text = describeEvent(digest);
    expect(text).toContain("2");
    expect(text).toContain("12 k");
    expect(text).not.toContain("{");
  });

  it("adds the servers' part to the digest of who reads the servers (KD-9057)", () => {
    const params = { decisions: 1, done: 2 };
    const incidents = { crit: 2, warn: 1, opened: 3, closed: 1, list: " · P-12, P-9" };
    const text = describeEvent(event("digest", { key: "push.digest.body.team", params, incidents }, "system"));
    expect(text).toBe("Ждут решения: 1 · сделано: 2 · Инциденты: crit 2 · warn 1 · за сутки открыто 3, закрыто 1 · P-12, P-9");
    expect(describeEvent(event("digest", { key: "push.digest.body.team", params }, "system"))).toBe("Ждут решения: 1 · сделано: 2");
  });

  it("describes an incident's steps with its P-n (KD-9057)", () => {
    const opened = { incident: 12, id: "P-12", server: "node-c", check: "disk", subject: "/", level: "crit", value: "96%" };
    const e = { ...event("incident.opened", opened, "system:monitor"), card: "" };
    expect(describeEvent(e)).toBe("Новый инцидент Критично · node-c · Занятое место на диске · /: 96%");
    expect(incidentOf(e)).toBe("P-12");
    const kinds = ["incident.worse", "incident.better", "incident.fixed", "incident.fix_failed", "incident.resolved",
      "incident.acknowledged", "incident.muted", "incident.unmuted"];
    for (const kind of kinds) {
      // the monitor's import brings a fix without its card
      const text = describeEvent(event(kind, { incident: 12, id: "P-12", level: "crit", value: "97%", until: "2026-10-15T06:00:00Z" }));
      expect(text).not.toBe(kind);
      expect(text).not.toContain("{");
    }
    expect(describeEvent(event("incident.fixed", { id: "P-12", card: "KD-5" }))).toBe("Исправление по KD-5");
    expect(incidentOf(event("card.created", { id: "P-1" }))).toBe("");
  });

  it("says an incident closed because its check was switched off, and a save of the monitor's settings (KD-9058)", () => {
    expect(describeEvent(event("incident.resolved", { id: "P-12", how: "close" }))).toBe("Инцидент закрыт");
    expect(describeEvent(event("incident.resolved", { id: "P-12", how: "disabled" })))
      .toBe("Инцидент закрыт: проверка выключена");
    const changes = [{ key: "disk_pct.warn", server: "" }, { key: "disabled_checks", server: "node-c" }];
    expect(describeEvent(event("monitor.settings", { changes, signed: true }, "user:owner")))
      .toBe("Настройки монитора изменены с подтверждением: disk_pct.warn, disabled_checks (node-c)");
    expect(describeEvent(event("monitor.settings", { changes: changes.slice(0, 1), signed: false }, "user:owner")))
      .toBe("Настройки монитора изменены: disk_pct.warn");
  });

  it("describes a notice event in words (KD-9028)", () => {
    const recovered = event("notice", { source: "watch", level: "warn", key: "notice.watch.recovered", params: {} }, "system:watch");
    expect(describeEvent(recovered)).toBe("⚠️ Состояние заторов было повреждено: отложено в сторону и собрано заново.");
  });

  it("names the plan step's card on an incident it changed (KD-9196)", () => {
    expect(describeEvent(event("incident.window", { incident: 7, id: "P-7", card: "KD-12" }, "system:monitor")))
      .toBe("Изменено шагом плана KD-12");
  });

  it("an unknown notice key falls back", () => {
    expect(describeEvent(event("notice", { key: "notice.none.such", params: {} }, "system:watch"))).toBe("Уведомление системы");
  });

  it("tells a role's permissions from a person's roles", () => {
    expect(describeEvent(event("user.role_set", { role: "viewer", perms: [] }, "user:owner"))).toBe("Изменена роль viewer");
    expect(describeEvent(event("user.role_set", { user: "anna", roles: [] }, "user:owner"))).toBe("Изменены роли @anna");
  });

  it("names whom a sign-in code was issued to and until when (KD-9091)", () => {
    const text = describeEvent(event("user.code_issued", { user: "anna", expires: "2026-10-15T09:15:00Z" }, "user:owner"));
    expect(text).toMatch(/^Выдан код входа для @anna — действует до \S/);
    expect(text).not.toContain("{");
  });

  it("falls back to the kind it does not know", () => {
    expect(describeEvent(event("something.new"))).toBe("something.new");
  });

  it("names actors", () => {
    expect(actorName("user:owner")).toBe("@owner");
    expect(actorName("agent:dev")).toBe("@dev");
    expect(actorName("system")).toBe("Система");
    expect(actorName("system:diag")).toBe("Система"); // final review never a raw principal
  });

  it("names a worker job's steps (KD-9025)", () => {
    const job = { job: "a".repeat(32), agent: "dev", state: "queued", spec: "reviewer" };
    expect(describeEvent(event("job.queued", job, "agent:boss"))).toBe("Рабочее задание reviewer для @dev — в очереди");
    for (const kind of ["job.started", "job.finished", "job.cancelling", "job.cancelled", "job.requeued"]) {
      const text = describeEvent(event(kind, { ...job, slot: "dev/worker1" }, "system"));
      expect(text).not.toBe(kind);
      expect(text).toContain("@dev");
    }
  });
});

describe("the runtime's steps in words (final review KD-9025)", () => {
  it("names every command, turn and session event", () => {
    const kinds = ["command.queued", "command.delivered", "command.done", "command.failed", "command.cancelled",
      "run.started", "run.finished", "run.failed", "run.waiting_user", "session.changed", "session.failed"];
    for (const kind of kinds) {
      const text = describeEvent(event(kind, { agent: "dev", command: "c", kind: "assigned", state: "idle" }, "system"));
      expect(text).not.toBe(kind);
      expect(text).toContain("@dev");
    }
  });
});

describe("echo messages", () => {
  it("a thread message another write stands for is an echo", () => {
    expect(isEcho({ seq: 1, kind: "message.added", payload: { via: "decision" } })).toBe(true);
    expect(isEcho({ seq: 2, kind: "message.added", payload: { kind: "text" } })).toBe(false);
    // the transcription line's own steps; a file deleted stays a line of the history
    expect(isEcho({ seq: 3, kind: "voice.queued", payload: { seq: 4 } })).toBe(true);
    expect(isEcho({ seq: 4, kind: "message.changed", payload: { seq: 4, voice: true } })).toBe(true);
    expect(isEcho({ seq: 5, kind: "message.changed", payload: { seq: 4, file: "x" } })).toBe(false);
    expect(isEcho({ seq: 3, kind: "card.changed", payload: { via: "x" } })).toBe(false);
  });
});
