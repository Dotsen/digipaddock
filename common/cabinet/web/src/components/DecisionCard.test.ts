import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import { FORM } from "../lib/questions.samples";
import { session, type Me } from "../lib/session.svelte";
import type { Decision, DecisionItem, DecisionView, PrCouncil } from "../lib/types";
import DecisionCard from "./DecisionCard.svelte";

vi.mock("../lib/webauthn", async (original) => ({
  ...(await original<typeof import("../lib/webauthn")>()),
  assertPasskey: vi.fn(async () => ({ id: "cred" })),
}));

function me(permissions: Record<string, string[]>): Me {
  return {
    id: "owner",
    name: "Владелец",
    language: "ru",
    timezone: "Etc/GMT-3",
    permissions,
    projects: ["admin", "alert"],
    csrf: "c",
    push_public_key: "k",
    second_factor: { passkey: true, totp: false, password: false },
    must_setup: false,
  };
}

function item(type: string, over: Partial<Decision> = {}, view: DecisionView = {}): DecisionItem {
  return {
    decision: {
      id: "0123456789abcdef0123456789abcdef",
      type,
      card: "KD-2",
      project: "admin",
      state: "open",
      step_up: type.startsWith("admin"),
      decide_permission: `decide.${type}`,
      payload: {},
      created: "2026-10-15T11:00:00Z",
      expires: "2099-01-01T00:00:00Z",
      requested_by: "agent:sysadmin",
      decided_by: "",
      decided_at: "",
      signature: null,
      reply: "",
      question: "",
      ...over,
    },
    view,
  };
}

const PLAN: DecisionView = {
  intent: "Перезапустить nginx",
  basis: "502",
  stale: false,
  ttl_options: [24, 72, 168],
  steps: [
    {
      n: 1,
      what: "Перезапустить веб-сервер",
      mechanics: "service.restart nginx.service",
      effect: "Сайт недоступен пару секунд",
      risk: { level: "medium", why: "Конфигурация" },
      rollback: "Вернуть конфигурацию",
      command: "systemctl restart nginx.service",
      servers: ["web-1"],
      production: true,
    },
  ],
};

const buttons = () => screen.queryAllByRole("button").map((b) => b.textContent?.trim());

const formItem = (over: Partial<Decision> = {}) =>
  item(
    "question",
    { step_up: false, expires: "", project: "alert", payload: FORM, ...over },
    { text: FORM.text, form: FORM, origin: "agent", form_sha256: "sha1" },
  );

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

beforeEach(() => setLocale("ru"));
afterEach(() => {
  cleanup();
  session.clear();
  sessionStorage.clear();
  vi.unstubAllGlobals();
});

describe("DecisionCard", () => {
  it("offers sign, ask and reject to whoever may decide an open plan", () => {
    session.signedIn(me({ "*": ["*"] }));
    render(DecisionCard, { item: item("admin_plan", {}, PLAN) });
    expect(buttons()).toEqual(expect.arrayContaining(["Подписать", "Задать вопрос", "Отклонить"]));
    expect(screen.getByText("Перезапустить веб-сервер")).toBeTruthy();
    expect(screen.getByText("прод")).toBeTruthy();
  });

  it("a question on the decision names the agent it went to (KD-9203)", () => {
    session.signedIn(me({ "*": ["*"] }));
    render(DecisionCard, { item: item("promote", { requested_by: "agent:boss", question: "Почему сейчас?" }, {}) });
    expect(screen.getByText("Вопрос @boss: Почему сейчас?")).toBeTruthy();
  });

  it("a withdrawn decision keeps its note for the agent off the page (KD-9203)", () => {
    // the thread's line says why in the person's words; the reply is the system's note to BOSS
    session.signedIn(me({ "*": ["*"] }));
    const reply = "the card changed after the proposal (assignee, labels) by @boss: propose it again";
    render(DecisionCard, { item: item("promote", { state: "revoked", decided_by: "agent:boss", reply }, {}) });
    expect(document.body.textContent).not.toContain("the card changed");
  });

  it("offers no question where no agent would read it", () => {
    // a question wakes the agent that asked, on its card; the monitor's trust has neither
    session.signedIn(me({ "*": ["*"] }));
    render(DecisionCard, { item: item("monitor_trust", { card: "", requested_by: "user:owner" }, { kind: "trust", cidr: "10.0.0.0/8" }) });
    expect(buttons()).toEqual(expect.arrayContaining(["Одобрить", "Отклонить"]));
    expect(buttons()).not.toContain("Задать вопрос");
  });

  it("shows no answer buttons to someone without the permission", () => {
    session.signedIn(me({ alert: ["cards.read", "decide.promote"] }));
    render(DecisionCard, { item: item("admin_plan", {}, PLAN) });
    expect(buttons()).not.toContain("Подписать");
    expect(buttons()).not.toContain("Отклонить");
  });

  it("does not let a stale decision be signed", () => {
    session.signedIn(me({ "*": ["*"] }));
    render(DecisionCard, { item: item("admin_plan", {}, { ...PLAN, stale: true }) });
    expect(screen.getByText("Решение устарело: карточка или план изменились")).toBeTruthy();
    expect(buttons()).not.toContain("Подписать");
    expect(buttons()).toContain("Отклонить");
  });

  it("asks a promote to be approved without a signature", () => {
    session.signedIn(me({ alert: ["decide.promote"] }));
    render(DecisionCard, {
      item: item("promote", { project: "alert", step_up: false }, { title: "Счёт", reason: "ТЗ готово", acceptance: [] }),
    });
    expect(buttons()).toContain("Одобрить");
    expect(buttons()).not.toContain("Подписать");
  });

  it("a promote lists the operations its approval grants (KD-9088)", () => {
    session.signedIn(me({ alert: ["decide.promote"] }));
    const step = { verb: "agent.reinstall", target: "factory", agents: ["dev"], what: "Переустановить dev",
      effect: "Новые инструкции", risk: { level: "low", why: "Очередь сохраняется" }, rollback: "Предыдущая выкатка" };
    render(DecisionCard, {
      item: item("promote", { project: "alert", step_up: false },
        { title: "Счёт", reason: "ТЗ готово", acceptance: [], operations: [step] }),
    });
    expect(screen.getByText("Операции — их разрешает одобрение ТЗ")).toBeTruthy();
    expect(screen.getByText("Переустановить dev")).toBeTruthy();
  });

  it("a promote of a changed brief says why it cannot be approved", () => {
    session.signedIn(me({ alert: ["decide.promote"] }));
    render(DecisionCard, {
      item: item("promote", { project: "alert", step_up: false },
        { title: "Счёт", reason: "ТЗ готово", acceptance: [], stale: true, brief_stale: true }),
    });
    expect(screen.getByText(/ТЗ изменилось после готовности/)).toBeTruthy();
    expect(buttons()).not.toContain("Одобрить");
  });

  it("a promote while BOSS is off says so, offers no approval and reads itself again (KD-9221)", () => {
    vi.useFakeTimers();
    try {
      session.signedIn(me({ alert: ["decide.promote"] }));
      const onchanged = vi.fn();
      render(DecisionCard, {
        item: item("promote", { project: "alert", step_up: false, requested_by: "agent:boss" },
          { title: "Счёт", reason: "ТЗ готово", acceptance: [], blocked: "boss_off" }),
        onchanged,
      });
      expect(screen.getByRole("status").textContent).toContain("BOSS сейчас выключен");
      expect(document.body.textContent).not.toContain("Решение устарело");
      expect(buttons()).not.toContain("Одобрить");
      expect(buttons()).toEqual(expect.arrayContaining(["Задать вопрос", "Отклонить"]));
      vi.advanceTimersByTime(30_000);
      expect(onchanged).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("takes an answer to a question", () => {
    session.signedIn(me({ "*": ["*"] }));
    render(DecisionCard, { item: item("question", { step_up: false, expires: "" }, { text: "Кому показывать?" }) });
    expect(screen.getByText("Кому показывать?")).toBeTruthy();
    expect(screen.getByPlaceholderText("Ваш ответ…")).toBeTruthy();
    expect(buttons()).toContain("Ответить");
  });

  it("a question in the thread hands the answer to the composer", async () => {
    session.signedIn(me({ "*": ["*"] }));
    const onreply = vi.fn();
    render(DecisionCard, { item: item("question", { step_up: false, expires: "" }, { text: "Кому?" }), onreply });
    expect(screen.queryByRole("textbox")).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: "Ответить" }));
    expect(onreply).toHaveBeenCalledWith("0123456789abcdef0123456789abcdef");
  });

  it.each([false, true])("agrees immediately to a plain question (thread: %s)", async (inThread) => {
    session.signedIn(me({ "*": ["*"] }));
    const fetch = vi.fn(async () => json({}));
    vi.stubGlobal("fetch", fetch);
    const onchanged = vi.fn();
    const onreply = vi.fn();
    render(DecisionCard, {
      item: item("question", { step_up: false, expires: "" }, { text: "Продолжаем?" }),
      onchanged, ...(inThread ? { onreply } : {}),
    });
    await fireEvent.click(screen.getByRole("button", { name: "Согласен" }));
    await vi.waitFor(() => expect(onchanged).toHaveBeenCalledOnce());
    const [path, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(path).toBe("/api/decisions/0123456789abcdef0123456789abcdef/reply");
    expect(JSON.parse(String(init.body))).toEqual({ text: "Согласен" });
    expect(onreply).not.toHaveBeenCalled();
  });

  it("keeps agreement unavailable to someone without permission or for a structured form", () => {
    session.signedIn(me({ admin: ["cards.read"] }));
    render(DecisionCard, { item: item("question", {}, { text: "Продолжаем?" }) });
    expect(buttons()).not.toContain("Согласен");
    cleanup();
    session.signedIn(me({ "*": ["*"] }));
    render(DecisionCard, { item: formItem() });
    expect(buttons()).not.toContain("Согласен");
    cleanup();
    render(DecisionCard, { item: item("question", {}, { text: "Пришлите файл", expect: "file" }) });
    expect(buttons()).not.toContain("Согласен");
  });

  it("a reject reason is multi-line", async () => {
    session.signedIn(me({ "*": ["*"] }));
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    render(DecisionCard, { item: item("admin_plan", {}, PLAN) });
    await fireEvent.click(screen.getByRole("button", { name: "Отклонить" }));
    const field = screen.getByRole("textbox");
    expect(field.tagName).toBe("TEXTAREA");
    await fireEvent.input(field, { target: { value: "рано" } });
    await fireEvent.keyDown(field, { key: "Enter" });
    expect(fetch).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("says who signed a decided plan and offers to revoke it", () => {
    session.signedIn(me({ "*": ["*"] }));
    render(DecisionCard, {
      item: item(
        "admin_plan",
        {
          state: "approved",
          decided_by: "user:owner",
          decided_at: "2026-10-15T11:30:00Z",
          signature: { id: "cred", type: "public-key" },
        },
        PLAN,
      ),
    });
    expect(screen.getByText(/Одобрено/)).toBeTruthy();
    expect(screen.getByText(/подпись ключом ✓/)).toBeTruthy();
    expect(buttons()).toEqual(["Подробнее", "Отозвать"]);
  });

  it("a plan approved by the test-environment rule names the rule and can be revoked (KD-9343)", () => {
    session.signedIn(me({ "*": ["*"] }));
    render(DecisionCard, {
      item: item("admin_plan", { state: "approved", decided_by: "rule:test-env", decided_at: "2026-10-15T11:30:00Z" }, PLAN),
    });
    expect(screen.getByText(/Одобрено · правило тестовых стендов/)).toBeTruthy();
    expect(screen.queryByText(/подпись ключом/)).toBeNull();
    expect(buttons()).toEqual(["Подробнее", "Отозвать"]);
  });

  it("a decided proposal folds to its outcome; «Подробнее» opens the whole document", async () => {
    session.signedIn(me({ "*": ["*"] }));
    render(DecisionCard, {
      item: item(
        "promote",
        { state: "approved", decided_by: "user:owner", decided_at: "2026-10-15T11:30:00Z" },
        { description: "Длинное ТЗ", acceptance: [{ text: "Есть отчёт", checked: false }] },
      ),
    });
    expect(screen.getByText(/Одобрено/)).toBeTruthy();
    expect(screen.queryByText("Длинное ТЗ")).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: "Подробнее" }));
    expect(screen.getByText("Длинное ТЗ")).toBeTruthy();
    await fireEvent.click(screen.getByRole("button", { name: "Свернуть" }));
    expect(screen.queryByText("Длинное ТЗ")).toBeNull();
  });

  it("a linked decided decision opens unfolded", () => {
    session.signedIn(me({ "*": ["*"] }));
    render(DecisionCard, {
      item: item("promote", { state: "rejected", decided_by: "user:owner", decided_at: "2026-10-15T11:30:00Z" }, { description: "Длинное ТЗ" }),
      highlight: true,
    });
    expect(screen.getByText("Длинное ТЗ")).toBeTruthy();
  });

  it("an open form question renders the form and posts answers, comment and the form's hash", async () => {
    session.signedIn(me({ "*": ["*"] }));
    const fetch = vi.fn(async () => json({}));
    vi.stubGlobal("fetch", fetch);
    const onreply = vi.fn();
    render(DecisionCard, { item: formItem(), onreply });
    expect(buttons()).not.toContain("Ответить"); // the form is answered in the form, not in the thread's field
    await fireEvent.click(screen.getByRole("radio", { name: /SSH/ }));
    await fireEvent.input(screen.getByRole("textbox", { name: "Условия" }), { target: { value: "до пятницы" } });
    await fireEvent.click(screen.getByRole("button", { name: "Отправить ответы" }));
    await vi.waitFor(() => expect(fetch).toHaveBeenCalled());
    const [path, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(path).toBe("/api/decisions/0123456789abcdef0123456789abcdef/reply");
    expect(JSON.parse(String(init.body))).toEqual({
      answers: {
        method: { selected_option_ids: ["ssh"], custom_text: "" },
        extra: { selected_option_ids: [], custom_text: "" },
        details: { selected_option_ids: [], custom_text: "до пятницы" },
      },
      comment: "",
      form_sha256: "sha1",
    });
  });

  it("an answered form shows what was chosen", () => {
    session.signedIn(me({ "*": ["*"] }));
    const answer = {
      schema_version: 1 as const,
      origin: "agent" as const,
      form_sha256: "sha1",
      answers: { method: { selected_option_ids: ["ssh"], custom_text: "по ключу" } },
      comment: "Срок — неделя",
    };
    render(DecisionCard, {
      item: formItem({ state: "approved", decided_by: "user:owner", decided_at: "2026-10-15T11:30:00Z", answer, reply: "1. raw" }),
    });
    expect(screen.getByText("Как проверять?")).toBeTruthy();
    expect(screen.getByText(/SSH/)).toBeTruthy();
    expect(screen.getByText(/по ключу/)).toBeTruthy();
    expect(screen.getAllByText("нет ответа")).toHaveLength(2);
    expect(screen.getByText("Срок — неделя")).toBeTruthy();
    expect(screen.queryByText(/1\. raw/)).toBeNull();
  });

  it("a conflict keeps the draft and re-reads the decision", async () => {
    session.signedIn(me({ "*": ["*"] }));
    vi.stubGlobal("fetch", vi.fn(async () => json({ error: "conflict", message: "the question changed" }, 409)));
    const onchanged = vi.fn();
    render(DecisionCard, { item: formItem(), onchanged });
    await fireEvent.click(screen.getByRole("radio", { name: /SSH/ }));
    await fireEvent.input(screen.getByRole("textbox", { name: "Условия" }), { target: { value: "до пятницы" } });
    await fireEvent.click(screen.getByRole("button", { name: "Отправить ответы" }));
    await vi.waitFor(() => expect(onchanged).toHaveBeenCalled());
    expect((screen.getByRole("radio", { name: /SSH/ }) as HTMLInputElement).checked).toBe(true);
    expect(sessionStorage.getItem("kd.draft.owner.KD-2.form:0123456789abcdef0123456789abcdef:sha1")).toContain("до пятницы");
  });

  it("a refused answer (someone answered first) re-reads the decision too", async () => {
    session.signedIn(me({ "*": ["*"] }));
    vi.stubGlobal("fetch", vi.fn(async () => json({ error: "refused", message: "already answered" }, 403)));
    const onchanged = vi.fn();
    render(DecisionCard, { item: formItem(), onchanged });
    await fireEvent.click(screen.getByRole("radio", { name: /SSH/ }));
    await fireEvent.input(screen.getByRole("textbox", { name: "Условия" }), { target: { value: "до пятницы" } });
    await fireEvent.click(screen.getByRole("button", { name: "Отправить ответы" }));
    await vi.waitFor(() => expect(onchanged).toHaveBeenCalled());
  });

  it("marks a critical command once and shows the command itself", () => {
    session.signedIn(me({ "*": ["*"] }));
    render(DecisionCard, {
      item: item(
        "admin_critical",
        {},
        {
          intent: "Убрать пир",
          command: "wg set wg0 peer 'abc=' remove",
          servers: ["db-1"],
          risk: { level: "high", why: "связь" },
          effect: "Пир отключён",
          rollback: "Вернуть",
          production: false,
          stale: false,
        },
      ),
    });
    expect(screen.getByText("одноразово, 30 мин")).toBeTruthy();
    expect(screen.getByText("wg set wg0 peer 'abc=' remove")).toBeTruthy();
  });
});

const HEAD = "abc1234" + "0".repeat(33);
const PR_VIEW: DecisionView = {
  repo: "example/DigiPaddock",
  number: 12,
  title: "KD-2: таймаут",
  url: "https://forge/pulls/12",
  head: HEAD,
  release: true,
  state: "open",
  stale: false,
  truncated: false,
  files: [
    { path: "common/kd/x.py", status: "modified", additions: 3, deletions: 1 },
    { path: "common/systemd/a.service", status: "added", additions: 10, deletions: 0 },
  ],
  cage: ["common/systemd/a.service"],
};
const DIFF = [
  "diff --git a/common/kd/x.py b/common/kd/x.py",
  "--- a/common/kd/x.py",
  "+++ b/common/kd/x.py",
  "@@ -1,2 +1,2 @@",
  "-b = 2",
  "+b = 3",
  " d = 5",
].join("\n");
const prItem = (over: Partial<Decision> = {}, view: DecisionView = {}) =>
  item("pr_approve", { step_up: true, project: "digipaddock", requested_by: "agent:dev", ...over }, { ...PR_VIEW, ...view });

describe("DecisionCard: a pull request", () => {
  it("shows the head, the files and the cage", () => {
    session.signedIn(me({ "*": ["*"] }));
    render(DecisionCard, { item: prItem() });
    expect(screen.getByText(/PR #12 · head abc1234/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "открыть в Forgejo ↗" }).getAttribute("href")).toBe("https://forge/pulls/12");
    expect(screen.getByText("common/systemd/a.service")).toBeTruthy();
    expect(screen.getAllByText("клетка")).toHaveLength(1);
    expect(screen.getByText(/Клетка: да — файлов: 1/)).toBeTruthy();
    cleanup();
    render(DecisionCard, { item: prItem({}, { cage: [] }) });
    expect(screen.getByText("Клетка: нет")).toBeTruthy();
  });

  it("a PR decision offers one approval with merge, release as a tick, and remarks", () => {
    // the owner, 19.10: three approve buttons were one too many — approve-only is gone, release is "and release"
    session.signedIn(me({ "*": ["*"] }));
    render(DecisionCard, { item: prItem() });
    expect(buttons()).toEqual(expect.arrayContaining(["Одобрить и merge", "Замечания"]));
    expect(buttons()).not.toContain("Одобрить");
    expect(buttons()).not.toContain("Одобрить, merge и release");
    expect(buttons()).not.toContain("Задать вопрос");
    expect(buttons()).not.toContain("Подписать");
    // the owner, 26.11 (KD-9458): the release to stage is ticked by default and may be unticked
    const tick = screen.getByRole("checkbox", { name: "и выкатить на stage" }) as HTMLInputElement;
    expect(tick.checked).toBe(true);
    cleanup();
    render(DecisionCard, { item: prItem({}, { release: false }) });
    expect(buttons()).toContain("Одобрить и merge");
    expect(screen.queryByRole("checkbox", { name: "и выкатить на stage" })).toBeNull();
  });

  it("a chain plan with several pull requests says the release is one, after the last merge", () => {
    session.signedIn(me({ "*": ["*"] }));
    render(DecisionCard, { item: prItem({}, { plan_pulls: 2 }) });
    expect(document.body.textContent).toContain("выкатка одна — после слияния всех PR плана");
    cleanup();
    render(DecisionCard, { item: prItem({}, { plan_pulls: 1 }) });
    expect(document.body.textContent).not.toContain("выкатка одна");
  });

  it("a signature carried to an updated head names the head it was given on and keeps its mode", () => {
    session.signedIn(me({ "*": ["*"] }));
    const carried = { decision: "d".repeat(32), head: "f".repeat(40), mode: "merge" as const };
    render(DecisionCard, {
      item: prItem({ state: "approved", signature: null, decided_by: "user:owner" }, { carried, target: "main" }),
      highlight: true,
    });
    expect(document.body.textContent).toContain("Подпись перенесена с fffffff после обновления от main");
    expect(document.body.textContent).toContain("Одобрено · merge");
    cleanup();
    render(DecisionCard, { item: prItem({}, {}) });
    expect(document.body.textContent).not.toContain("Подпись перенесена");
  });

  it("the button signs merge and release with the tick, or merge with it unticked, with a passkey only", async () => {
    session.signedIn(me({ "*": ["*"] }));
    render(DecisionCard, { item: prItem() });
    await fireEvent.click(screen.getByRole("button", { name: "Одобрить и merge" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog.textContent).toContain("одобрение, merge и release");
    expect(dialog.textContent).not.toContain("Ввести код TOTP");
    cleanup();
    render(DecisionCard, { item: prItem() });
    await fireEvent.click(screen.getByRole("checkbox", { name: "и выкатить на stage" }));
    await fireEvent.click(screen.getByRole("button", { name: "Одобрить и merge" }));
    expect(screen.getByRole("dialog").textContent).toContain("одобрение и merge");
    cleanup();
    // no release from here: the button signs merge whatever the tick holds
    render(DecisionCard, { item: prItem({}, { release: false }) });
    await fireEvent.click(screen.getByRole("button", { name: "Одобрить и merge" }));
    expect(screen.getByRole("dialog").textContent).toContain("одобрение и merge");
  });

  it("a moved PR offers no approval, only remarks", () => {
    session.signedIn(me({ "*": ["*"] }));
    render(DecisionCard, { item: prItem({}, { stale: true, files: [], cage: [] }) });
    expect(screen.getByText(/PR изменился после публикации/)).toBeTruthy();
    expect(buttons()).not.toContain("Одобрить и merge");
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(buttons()).toContain("Замечания");
  });

  it("remarks send text and comments", async () => {
    session.signedIn(me({ "*": ["*"] }));
    const fetch = vi.fn(async (url: string) =>
      url.endsWith("/diff") ? json({ diff: DIFF, truncated: false, head: HEAD }) : json({ decision: {}, forge: "posted" }),
    );
    vi.stubGlobal("fetch", fetch);
    const onchanged = vi.fn();
    render(DecisionCard, { item: prItem(), onchanged });
    await fireEvent.click(screen.getByRole("button", { name: "Показать дифф" }));
    await screen.findByText("b = 3");
    await fireEvent.click(screen.getByRole("button", { name: "Скрыть дифф" }));
    await fireEvent.click(screen.getByRole("button", { name: "Показать дифф" }));
    await fireEvent.click(screen.getByRole("button", { name: "Замечания" }));
    await fireEvent.click(screen.getByRole("button", { name: "Замечание к строке 2 (новая)" }));
    await fireEvent.input(screen.getByRole("textbox", { name: "Замечание к строке" }), { target: { value: "зачем 3?" } });
    await fireEvent.click(screen.getByRole("button", { name: "Добавить" }));
    await fireEvent.input(screen.getByPlaceholderText("Что поправить…"), { target: { value: "поправь тесты" } });
    await fireEvent.click(screen.getByRole("button", { name: "Отправить замечания" }));
    await vi.waitFor(() => expect(onchanged).toHaveBeenCalled());
    const calls = fetch.mock.calls as unknown as [string, RequestInit | undefined][];
    expect(calls.filter(([url]) => url.endsWith("/diff"))).toHaveLength(1); // the diff is read once
    const [path, init] = calls.find(([url]) => url.endsWith("/reject"))!;
    expect(path).toBe("/api/decisions/0123456789abcdef0123456789abcdef/reject");
    expect(JSON.parse(String(init?.body))).toEqual({
      text: "поправь тесты",
      comments: [{ path: "common/kd/x.py", line: 2, side: "new", body: "зачем 3?" }],
    });
  });

  it("remarks over the limit say so and are not sent", async () => {
    // final review: the text the thread records (text + the line remarks) is held to the backend's 4000
    session.signedIn(me({ "*": ["*"] }));
    render(DecisionCard, { item: prItem() });
    await fireEvent.click(screen.getByRole("button", { name: "Замечания" }));
    await fireEvent.input(screen.getByPlaceholderText("Что поправить…"), { target: { value: "x".repeat(4001) } });
    expect(screen.getByText("4001 / 4000")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Отправить замечания" }).hasAttribute("disabled")).toBe(true);
    await fireEvent.input(screen.getByPlaceholderText("Что поправить…"), { target: { value: "x".repeat(4000) } });
    expect(screen.getByRole("button", { name: "Отправить замечания" }).hasAttribute("disabled")).toBe(false);
  });

  it("an Approve already in Forgejo and a slow kd-review say what happened", async () => {
    session.signedIn(me({ "*": ["*"] }));
    const challenge = { challenge_id: "c1", options: { challenge: "v" }, step_up: true, totp: false,
                        expires: "2026-10-19T10:05:00Z", revisions: {}, mode: "merge" };
    for (const [answer, status, text] of [
      [{ error: "approved_in_forge", message: "x" }, 409, /Одобрение уже в Forgejo/],
      [{ error: "review_timeout", message: "x" }, 503, /kd-review не ответил вовремя/],
    ] as const) {
      vi.stubGlobal("fetch", vi.fn(async (url: string) => (url.endsWith("/challenge") ? json(challenge) : json(answer, status))));
      render(DecisionCard, { item: prItem() });
      await fireEvent.click(screen.getByRole("button", { name: "Одобрить и merge" }));
      await fireEvent.click(screen.getByRole("button", { name: "Подписать" }));
      expect(await screen.findByText(text)).toBeTruthy();
      cleanup();
    }
  });

  it("a decided PR says the mode it was signed with, or that remarks were sent", () => {
    session.signedIn(me({ "*": ["*"] }));
    const decided = { decided_by: "user:owner", decided_at: "2026-10-15T11:30:00Z" };
    render(DecisionCard, {
      item: prItem({ ...decided, state: "approved", signature: { type: "public-key", binding: { mode: "release" } } }),
    });
    expect(screen.getByText(/Одобрено · merge и release/)).toBeTruthy();
    expect(buttons()).not.toContain("Отозвать");
    cleanup();
    render(DecisionCard, { item: prItem({ ...decided, state: "rejected", reply: "поправь тесты" }) });
    expect(screen.getByText(/Замечания отправлены/)).toBeTruthy();
  });

  // the passport's branch, what a merge into it sets off, and the checks green on the head
  it("shows the target and the checks", () => {
    session.signedIn(me({ "*": ["*"] }));
    render(DecisionCard, { item: prItem({}, { target: "master", checks: ["unit", "static"] }) });
    expect(screen.getByText(/PR #12 · head abc1234 · в ветку master/)).toBeTruthy();
    expect(screen.getByText("Проверки: unit, static")).toBeTruthy();
    cleanup();
    render(DecisionCard, { item: prItem({}, { target: "main", checks: [] }) });
    expect(screen.getByText("Обязательных проверок нет")).toBeTruthy();
  });

  it("a prod effect is red and the button says prod", () => {
    session.signedIn(me({ "*": ["*"] }));
    render(DecisionCard, { item: prItem({}, { target: "main", effect: "Forgejo Actions: деплой на prod" }) });
    const line = screen.getByText("После merge: Forgejo Actions: деплой на prod");
    expect(line.classList.contains("crit")).toBe(true);
    expect(buttons()).toContain("Одобрить и merge в prod");
    expect(buttons()).not.toContain("Одобрить и merge");
    cleanup();
    render(DecisionCard, { item: prItem({}, { target: "master", effect: "деплой на stage приёмки" }) });
    const stage = screen.getByText("После merge: деплой на stage приёмки");
    expect(stage.classList.contains("warn")).toBe(true);
    expect(buttons()).toContain("Одобрить и merge");
  });

  it("no effect — no line", () => {
    session.signedIn(me({ "*": ["*"] }));
    render(DecisionCard, { item: prItem({}, { target: "main", effect: "" }) });
    expect(screen.queryByText(/После merge/)).toBeNull();
  });

  // what the card's council of the code said of this head
  it("says how the council's review of the head ended and links to its block", () => {
    session.signedIn(me({ "*": ["*"] }));
    const said = (state: PrCouncil["state"], over: Partial<PrCouncil> = {}): PrCouncil => ({ state, council: "c1", lines: 150, min_lines: 100, rejected: 0, ...over });
    const cases: [PrCouncil, string, string][] = [
      [said("clean"), "Ревью совета: чисто", "muted"],
      [said("settled", { rejected: 2 }), "Ревью совета: отклонено автором: 2", "warn"],
      [said("limited"), "Ревью совета: предел кругов", "warn"],
      [said("failed"), "Ревью совета: не состоялось", "warn"],
      [said("cancelled"), "Ревью совета: отменено", "muted"],
    ];
    for (const [council, words, tone] of cases) {
      render(DecisionCard, { item: prItem({ card: "KD-2" }, { council }) });
      const link = screen.getByRole("link", { name: words });
      expect(link.getAttribute("href")).toBe("/cards/KD-2");
      expect(link.closest("p")?.classList.contains(tone)).toBe(true);
      cleanup();
    }
  });

  it("a head nobody reviewed says why; a card without critics of the code says nothing", () => {
    session.signedIn(me({ "*": ["*"] }));
    const none = { council: null, rejected: 0, min_lines: 100 };
    render(DecisionCard, { item: prItem({}, { council: { state: "skipped", lines: 10, ...none } }) });
    expect(screen.getByText("Ревью совета: не требуется (строк кода: 10, порог 100)")).toBeTruthy();
    expect(screen.queryByRole("link", { name: /Ревью совета/ })).toBeNull();
    cleanup();
    render(DecisionCard, { item: prItem({}, { council: { state: "none", lines: 150, ...none } }) });
    expect(screen.getByText("Ревью совета: не проводилось")).toBeTruthy();
    cleanup();
    render(DecisionCard, { item: prItem() });
    expect(screen.queryByText(/Ревью совета/)).toBeNull();
  });

  it("the link scrolls to the council's block when the page has it", async () => {
    session.signedIn(me({ "*": ["*"] }));
    const block = document.createElement("section");
    block.id = "council-c1";
    block.scrollIntoView = vi.fn();
    document.body.append(block);
    render(DecisionCard, { item: prItem({ card: "KD-2" }, { council: { state: "clean", council: "c1", lines: 150, min_lines: 100, rejected: 0 } }) });
    const click = new MouseEvent("click", { bubbles: true, cancelable: true });
    screen.getByRole("link", { name: "Ревью совета: чисто" }).dispatchEvent(click);
    expect(block.scrollIntoView).toHaveBeenCalled();
    expect(click.defaultPrevented).toBe(true);
    block.remove();
  });
});
