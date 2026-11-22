import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import { session } from "../lib/session.svelte";
import { thread } from "../lib/stores/thread.svelte";
import type { DecisionItem, Message as MessageType } from "../lib/types";
import Message from "./Message.svelte";

const status = (key: string, params: Record<string, string>, body = "the session failed"): MessageType => ({
  seq: 5, author: "system:session-host", author_name: "", kind: "status", body,
  payload: { key, params }, created: "2026-10-17T10:00:00Z",
});

beforeEach(() => setLocale("ru"));
afterEach(cleanup);

describe("Message", () => {
  it("keeps instruction and memory events visible as separate blocks in the conversation", () => {
    const event = (key: string, activity: Record<string, unknown>) => ({ ...status(key, {}), payload: { key, activity } });
    const { container } = render(Message, { message: event("sys.session.instructions_loaded", {
      type: "instructions", state: "loaded", spec: "docs", version: 1,
      skills: { "technical-writing": { version: 1 } },
    }) });
    expect(container.querySelector(".activity")).not.toBeNull();
    expect(screen.getByText("docs v1")).toBeTruthy();
    expect(screen.getByText("technical-writing v1")).toBeTruthy();
    cleanup();
    render(Message, { message: event("sys.session.memory", {
      type: "memory", state: "failed", operation: "search_knowledge_pages", mode: "read",
    }) });
    expect(screen.getByText("Hindsight")).toBeTruthy();
    expect(screen.getByText("Поиск по памяти · ошибка вызова")).toBeTruthy();
    expect(document.querySelector(".activity.failed")).not.toBeNull();
    expect(document.body.textContent).not.toContain("сохранён");
  });

  it("a help result names its helper, state, source and the limit of the check (KD-9071)", () => {
    const help = (payload: Record<string, unknown>, body = "333 из 333"): MessageType => ({
      seq: 7, author: "agent:sysadmin", author_name: "sysadmin", kind: "assist", body,
      payload, created: "2026-10-22T08:22:56Z",
    });
    render(Message, { message: help({ assist: "c".repeat(32), state: "done", reason: "", limit: "только номера",
                                      source: "reported", late: false }) });
    let text = document.body.textContent ?? "";
    expect(text).toContain("Помощь · выполнена · сообщил sysadmin");
    expect(text).toContain("333 из 333");
    expect(text).toContain("Предел проверки: только номера");
    cleanup();
    render(Message, { message: help({ assist: "c".repeat(32), state: "failed", reason: "capability_missing", limit: "",
                                      source: "reported", late: true }, "нет доступа") });
    text = document.body.textContent ?? "";
    expect(text).toContain("Помощь · не выполнена (нет нужного инструмента) · сообщил sysadmin · поздний результат");
  });

  it("a session's failure line says the error in words (KD-229 5a)", () => {
    render(Message, { message: status("sys.session.failed", { agent: "@analyst", error: "exit 143" }) });
    const text = document.body.textContent ?? "";
    expect(text).toContain("Сессия @analyst завершилась сбоем: процесс сессии завершился (код 143)");
    expect(text).not.toContain("exit 143");
  });

  it("renders a diagnostic result with code, target and requester (R31)", () => {
    const diagnostic = (key: string, params: Record<string, string>): MessageType => ({
      seq: 6, author: "system:diag", author_name: "", kind: "diagnostic", body: "",
      payload: { key, params, diag: { target: "hindsight-api", op: "http-health", key: "hindsight-api" } },
      created: "2026-10-17T10:00:00Z",
    });
    const at = "2026-10-17T10:00:00Z";
    render(Message, { message: diagnostic("diag.result.http", { target: "hindsight-api", status: "503", time: at, agent: "@dev" }) });
    let text = document.body.textContent ?? "";
    expect(text).toContain("Диагностика hindsight-api: HTTP 503");
    expect(text).toContain("по запросу @dev");
    expect(text).not.toContain(at);
    cleanup();
    render(Message, { message: diagnostic("diag.result.error", { target: "hindsight-api", category: "refused", time: at, agent: "@dev" }) });
    text = document.body.textContent ?? "";
    expect(text).toContain("Диагностика hindsight-api: соединение отклонено");
    cleanup();
    render(Message, { message: diagnostic("diag.missing", { target: "mysql" }) });
    expect(document.body.textContent).toContain("Диагностики mysql нет в каталоге — нужна платформенная работа (@boss)");
  });

  const notice = (level: string, key: string, params: Record<string, string>): MessageType => ({
    seq: 7, author: "system:exec", author_name: "", kind: "notice", body: "",
    payload: { key, params, level, source: "exec" }, created: "2026-10-18T10:00:00Z",
  });

  it("a notice is its catalog text, marked by its level (KD-9028)", () => {
    const { container } = render(Message, {
      message: notice("warn", "notice.exec.denied", { agent: "@sysadmin", server: "node-a", rule: "rm-root", command: "rm -rf /" }),
    });
    expect(document.body.textContent).toContain("⛔ @sysadmin · node-a · отказ: правило rm-root: rm -rf /");
    expect(container.querySelector(".notice.warn")).not.toBeNull();
  });

  it("every message shows its clock time, not how long ago (KD-9036)", () => {
    const created = new Date(new Date().setHours(9, 7, 0, 0)).toISOString();
    const text = (m: MessageType) => {
      cleanup();
      render(Message, { message: m });
      return document.body.textContent ?? "";
    };
    const bubble: MessageType = { seq: 8, author: "agent:dev", author_name: "@dev", kind: "text", body: "Готово", payload: {}, created };
    expect(text(bubble)).toContain("09:07");
    expect(text(bubble)).not.toContain("мин");
    expect(text({ ...status("sys.session.failed", { agent: "@dev", error: "x" }), created })).toContain("09:07");
    expect(text({ ...notice("info", "notice.forge.merged", { pr: "1", title: "x" }), created })).toContain("09:07");
  });

  it("notice params render as text", () => {
    const { container } = render(Message, {
      message: notice("info", "notice.forge.merged", { pr: "12", title: "<b>x</b>" }),
    });
    expect(container.querySelector("b")).toBeNull();
    expect(document.body.textContent).toContain("Слит PR #12: <b>x</b>");
  });

  describe("an answer to an agent's question (KD-9042)", () => {
    const READABLE = "1. Что попадает во Входящие?: Только критичные — и решения\n2. Раздел «Серверы»?: Да\n\nСпасибо";
    const replied = (body: string): MessageType => ({
      seq: 9, author: "user:owner", author_name: "@owner", kind: "decision", body,
      payload: { decision: "q1", type: "question", state: "approved", key: "sys.decision.replied", params: { by: "@owner" }, text: body },
      created: "2026-10-17T10:00:00Z",
    });
    const item = {
      decision: {
        id: "q1", type: "question", state: "approved", reply: READABLE,
        answer: { schema_version: 1, origin: "agent", form_sha256: "x", comment: "Спасибо", answers: {
          a: { selected_option_ids: ["a1"], custom_text: "и решения" },
          b: { selected_option_ids: ["b1"], custom_text: "" },
        } },
      },
      view: { form: { schema_version: 1, text: "", questions: [
        { id: "a", title: "Что попадает во Входящие?", context: "", type: "single_select", required: true, allow_custom: true,
          options: [{ id: "a1", label: "Только критичные", description: "", recommended: true }] },
        { id: "b", title: "Раздел «Серверы»?", context: "", type: "single_select", required: true, allow_custom: false,
          options: [{ id: "b1", label: "Да", description: "", recommended: true }] },
      ] } },
    } as unknown as DecisionItem;

    it("a form's answer reads question by question, the comment after them", () => {
      render(Message, { message: replied(READABLE), decision: item });
      expect(document.querySelector(".sys .body")?.textContent).toContain("@owner ответил");
      const rows = [...document.querySelectorAll(".answer li")].map((li) => [
        li.querySelector(".q")?.textContent?.trim(), li.querySelector(".a")?.textContent?.trim()]);
      expect(rows).toEqual([["Что попадает во Входящие?", "Только критичные — и решения"], ["Раздел «Серверы»?", "Да"]]);
      expect(document.querySelector(".answer .comment")?.textContent).toBe("Спасибо");
      expect(document.body.textContent).not.toContain("«1.");
    });

    it("without the form (an agent's chat) the text keeps its lines in a block", () => {
      render(Message, { message: replied(READABLE) });
      const block = document.querySelector(".answer.text") as HTMLElement;
      expect(block.textContent).toBe(READABLE);
      expect(block.closest(".msg.sys")).not.toBeNull(); // `.answer.text` keeps the lines (white-space: pre-line)
    });

    it("a short one-line answer stays in the line", () => {
      render(Message, { message: replied("да, берём") });
      expect(document.querySelector(".sys .body")?.textContent).toContain("@owner ответил: «да, берём»");
      expect(document.querySelector(".answer")).toBeNull();
    });
  });
});

describe("Message files (KD-9041)", () => {
  const PNG = { id: "a".repeat(32), name: "shot.png", mime: "image/png", size: 2048 };
  const SVG = { id: "b".repeat(32), name: "logo.svg", mime: "image/svg+xml", size: 300 };
  const withFiles = (author: string, files: unknown[], body = "посмотри", kind = "text"): MessageType => ({
    seq: 9, author, author_name: "Анна", kind, body, payload: { files }, created: "2026-10-20T10:00:00Z",
  });
  const asMe = (id: string, permissions: Record<string, string[]>) =>
    session.signedIn({ id, name: id, language: "ru", timezone: "UTC", permissions, projects: [], csrf: "c",
      push_public_key: "", second_factor: { passkey: true, totp: false, password: false }, must_setup: false });
  afterEach(() => session.clear());

  it("a raster picture is a thumbnail that opens full screen; SVG is a download", async () => {
    asMe("anna", { alert: ["cards.comment"] });
    render(Message, { message: withFiles("user:anna", [PNG, SVG]) });
    expect(document.body.textContent).toContain("посмотри");
    const img = document.querySelector(".thumb img") as HTMLImageElement;
    expect(img.getAttribute("src")).toBe(`/api/files/${PNG.id}?inline=1`);
    const link = document.querySelector("a.file") as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe(`/api/files/${SVG.id}`);
    expect(document.querySelectorAll(".thumb")).toHaveLength(1);
    await fireEvent.click(screen.getByRole("button", { name: "Открыть «shot.png» во весь экран" }));
    expect(screen.getByRole("dialog")).toBeTruthy();
    await fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("a voice message is a player in the thread (KD-9077)", () => {
    asMe("anna", {});
    const voice = { id: "d".repeat(32), name: "voice-20261020-213005.webm", mime: "audio/webm", size: 40960 };
    render(Message, { message: withFiles("user:anna", [voice], voice.name, "file") });
    const audio = document.querySelector("audio") as HTMLAudioElement;
    expect(audio.getAttribute("src")).toBe(`/api/files/${voice.id}?inline=1`);
    expect(audio.hasAttribute("controls")).toBe(true);
    expect((document.querySelector(".voice a") as HTMLAnchorElement).getAttribute("href")).toBe(`/api/files/${voice.id}`);
    expect(document.querySelector("a.file")).toBeNull();
    expect(document.querySelectorAll(".b p")).toHaveLength(0);
  });

  it("a file message shows its files without repeating their names as text; a KD-226 one still reads", () => {
    asMe("anna", {});
    render(Message, { message: withFiles("user:anna", [SVG], "logo.svg", "file") });
    expect(document.querySelectorAll(".b p")).toHaveLength(0);
    cleanup();
    const old: MessageType = { seq: 3, author: "user:anna", author_name: "Анна", kind: "file", body: "old.txt",
      payload: { id: "c".repeat(32), name: "old.txt", mime: "text/plain", size: 3 }, created: "2026-10-15T10:00:00Z" };
    render(Message, { message: old });
    expect((document.querySelector("a.file") as HTMLAnchorElement).getAttribute("href")).toBe(`/api/files/${"c".repeat(32)}`);
  });

  it("a deleted file says so; only the author or the owner sees delete", () => {
    asMe("boris", { alert: ["cards.comment"] });
    render(Message, { message: withFiles("user:anna", [{ id: PNG.id, name: "shot.png", deleted: true }, SVG]) });
    expect(document.body.textContent).toContain("shot.png — файл удалён");
    expect(document.querySelector(".thumb")).toBeNull();
    expect(screen.queryByRole("button", { name: "Удалить файл" })).toBeNull();
    cleanup();
    asMe("owner", { "*": ["*"] });
    render(Message, { message: withFiles("user:anna", [SVG]) });
    expect(screen.getByRole("button", { name: "Удалить файл" })).toBeTruthy();
  });

  it("delete asks first and puts the changed message into the thread", async () => {
    asMe("anna", {});
    const fetch = vi.fn(async (_url: string, _init?: RequestInit) => new Response(JSON.stringify(withFiles("user:anna", [{ ...SVG, size: undefined, mime: undefined, deleted: true }])),
      { headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetch);
    vi.stubGlobal("confirm", vi.fn(() => true));
    const add = vi.spyOn(thread, "add");
    render(Message, { message: withFiles("user:anna", [SVG]) });
    await fireEvent.click(screen.getByRole("button", { name: "Удалить файл" }));
    await vi.waitFor(() => expect(add).toHaveBeenCalled());
    expect(fetch.mock.calls[0][0]).toBe(`/api/files/${SVG.id}`);
    expect((fetch.mock.calls[0][1] as RequestInit).method).toBe("DELETE");
    vi.unstubAllGlobals();
    add.mockRestore();
  });
});

describe("an answer with files (KD-9082)", () => {
  it("decision line renders its files", () => {
    const answer: MessageType = {
      seq: 7, author: "user:owner", author_name: "Владелец", kind: "decision", body: "вот",
      payload: { decision: "q1", type: "question", state: "approved", key: "sys.decision.replied", params: { by: "Владелец" },
                 text: "вот", files: [{ id: "f".repeat(32), name: "notes.txt", mime: "text/plain", size: 5 }] },
      created: "2026-10-21T10:00:00Z",
    };
    render(Message, { message: answer });
    const link = screen.getByRole("link", { name: /notes\.txt/ }) as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe(`/api/files/${"f".repeat(32)}`);
  });
});
