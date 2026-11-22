import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { council } from "../lib/council.fixtures";
import { clearDrafts } from "../lib/drafts";
import { setLocale } from "../lib/i18n.svelte";
import { FORM } from "../lib/questions.samples";
import { session, type Me } from "../lib/session.svelte";
import type { Decision, DecisionItem, Message } from "../lib/types";
import { thread } from "../lib/stores/thread.svelte";
import { toasts } from "../lib/toast.svelte";
import Thread, { blocks, toolSteps } from "./Thread.svelte";

const msg = (seq: number, kind: string, payload: Record<string, unknown> = {}): Message => ({
  seq,
  author: "agent:dev",
  author_name: "@dev",
  kind,
  body: `m${seq}`,
  payload,
  created: "2026-10-15T12:00:00Z",
});

const me: Me = {
  id: "owner",
  name: "Владелец",
  language: "ru",
  timezone: "Etc/GMT-3",
  permissions: { "*": ["*"] },
  projects: ["digipaddock"],
  csrf: "c",
  push_public_key: "k",
  second_factor: { passkey: true, totp: false, password: false },
  must_setup: false,
};

function question(id: string, agent: string): DecisionItem {
  const decision: Decision = {
    id,
    type: "question",
    card: "KD-5",
    project: "digipaddock",
    state: "open",
    step_up: false,
    decide_permission: "decide.question",
    payload: { text: "?" },
    created: "2026-10-15T11:00:00Z",
    expires: "",
    requested_by: `agent:${agent}`,
    decided_by: "",
    decided_at: "",
    signature: null,
    reply: "",
    question: "",
  };
  return { decision, view: { text: "?" } };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function show(decisions: Record<string, DecisionItem> = {}, ondecision = vi.fn()) {
  render(Thread, { cardId: "KD-5", decisions, canComment: true, agents: ["@dev"], assignee: "@dev", ondecision });
  return { field: screen.getByRole("textbox") as HTMLTextAreaElement, ondecision };
}

const type = (field: HTMLTextAreaElement, value: string) => fireEvent.input(field, { target: { value } });
const send = (field: HTMLTextAreaElement) => fireEvent.keyDown(field, { key: "Enter", ctrlKey: true });
const posted = (fetch: ReturnType<typeof vi.fn>) =>
  fetch.mock.calls.map((c) => [c[0], JSON.parse(String(c[1]?.body ?? "{}"))]);

beforeEach(() => {
  setLocale("ru");
  session.signedIn(me);
});
afterEach(() => {
  cleanup();
  session.clear();
  clearDrafts(); // the stored drafts and the threads' copy of them
  vi.unstubAllGlobals();
});

describe("thread blocks", () => {
  it("shows an open decision under the newest message, a decided one where it was opened", () => {
    // the owner reads the agent's last word first, then acts on its PR right above the field
    const item = (id: string, state: string) => ({ decision: { id, state }, view: {} }) as unknown as DecisionItem;
    const shown = blocks(
      [
        msg(1, "decision", { decision: "pr", state: "open" }),
        msg(2, "decision", { decision: "done", state: "open" }),
        msg(3, "tool_call"),
        msg(4, "text"),
      ],
      { pr: item("pr", "open"), done: item("done", "approved") },
    );
    expect(shown.map((b) => [b.type, b.key])).toEqual([
      ["decision", "d2"],
      ["tools", "t3"],
      ["message", "m4"],
      ["decision", "d1"],
    ]);
  });

  it("puts a decision where it was opened and folds tool calls together", () => {
    const decisions = { d1: { decision: { id: "d1" }, view: {} } as unknown as DecisionItem };
    const shown = blocks(
      [
        msg(1, "text"),
        msg(2, "tool_call"),
        msg(3, "tool_result"),
        msg(4, "decision", { decision: "d1", state: "open" }),
        msg(5, "decision", { decision: "d1", state: "approved" }),
        msg(6, "tool_call"),
        msg(7, "decision", { decision: "gone", state: "open" }),
      ],
      decisions,
    );
    expect(shown.map((b) => [b.type, b.key])).toEqual([
      ["message", "m1"],
      ["tools", "t2"],
      ["decision", "d4"],
      ["message", "m5"],
      ["tools", "t6"],
      ["message", "m7"],
    ]);
    expect(shown[1].type === "tools" && shown[1].messages.map((m) => m.seq)).toEqual([2, 3]);
  });

  it("shows a question on an open decision as a line, the decision once", () => {
    // "Ask a question" kept the decision open with a second "open" message — the thread showed it twice
    const decisions = { d1: { decision: { id: "d1", state: "open" }, view: {} } as unknown as DecisionItem };
    const shown = blocks(
      [
        msg(1, "decision", { decision: "d1", state: "open", key: "sys.decision.opened" }),
        msg(2, "decision", { decision: "d1", state: "open", key: "sys.decision.asked" }),
      ],
      decisions,
    );
    expect(shown.map((b) => [b.type, b.key])).toEqual([
      ["message", "m2"],
      ["decision", "d1"],
    ]);
  });

  it("a council is one block: its reviews are no lines; an open one stands under the newest message (KD-9267)", () => {
    const item = { decision: { id: "q", state: "open" }, view: {} } as unknown as DecisionItem;
    const shown = blocks(
      [
        msg(1, "text"),
        msg(2, "review", { council: "c1" }),
        msg(3, "decision", { decision: "q", state: "open" }),
        msg(4, "review_reply", { council: "c1" }),
        msg(5, "text"),
      ],
      { q: item },
      [council({ id: "c1", state: "needs_writer" })],
    );
    // the open decision stays the last: it is answered from the field under it
    expect(shown.map((b) => [b.type, b.key])).toEqual([
      ["message", "m1"],
      ["message", "m5"],
      ["council", "cc1"],
      ["decision", "d3"],
    ]);
  });

  it("an ended council stands where its first message is; a review of a council not read stays a line", () => {
    const shown = blocks(
      [msg(1, "review", { council: "c0" }), msg(2, "text"), msg(3, "review", { council: "c0" }), msg(4, "review", { council: "zz" })],
      {},
      [council({ id: "c0", state: "clean" })],
    );
    expect(shown.map((b) => [b.type, b.key])).toEqual([
      ["council", "cc0"],
      ["message", "m2"],
      ["message", "m4"],
    ]);
  });

  it("an open council no critic has answered yet is shown too; an ended one outside the window is not", () => {
    const shown = blocks([msg(7, "text")], {}, [council({ id: "c2", state: "running" }), council({ id: "c0", state: "clean" })]);
    expect(shown.map((b) => [b.type, b.key])).toEqual([
      ["message", "m7"],
      ["council", "cc2"],
    ]);
  });
});

describe("the council in the thread (KD-9267)", () => {
  async function shown(props: Record<string, unknown> = {}) {
    const messages = [msg(1, "text"), { ...msg(2, "review", { council: "c1" }), body: "brief council round 1 · claude:opus" }];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json({ messages, watermark: 0 })));
    await thread.load("KD-5");
    render(Thread, {
      cardId: "KD-5", decisions: {}, canComment: true, agents: ["@dev"], assignee: "@dev", ondecision: vi.fn(),
      councils: [council()], ...props,
    });
  }

  it("shows the block instead of the review's line", async () => {
    await shown();
    const text = document.body.textContent ?? "";
    expect(text).toContain("Совет · ТЗ ред. 1 · круг 1 из 3");
    expect(text).not.toContain("brief council round 1 · claude:opus");
    expect(screen.queryByRole("button", { name: "Отменить совет" })).toBeNull();
  });

  it("whoever manages councils cancels this one and opens the composition from the block", async () => {
    const oncouncilcancel = vi.fn();
    const oncounciledit = vi.fn();
    await shown({ canManageCouncil: true, oncouncilcancel, oncounciledit });
    await fireEvent.click(screen.getByRole("button", { name: "Отменить совет" }));
    await fireEvent.click(screen.getByRole("button", { name: "Да, отменить" }));
    expect(oncouncilcancel).toHaveBeenCalledWith("c1");
    await fireEvent.click(screen.getByRole("button", { name: "Изменить состав" }));
    expect(oncounciledit).toHaveBeenCalledTimes(1);
  });
});

describe("thread composer", () => {
  it("Enter breaks the line and Ctrl+Enter sends", async () => {
    const fetch = vi.fn(async (_url: string, _init?: RequestInit) => json(msg(1, "text")));
    vi.stubGlobal("fetch", fetch);
    const { field } = show();
    await type(field, "привет");
    await fireEvent.keyDown(field, { key: "Enter" });
    expect(fetch).not.toHaveBeenCalled();
    await send(field);
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    const [[url, body]] = posted(fetch);
    expect(url).toBe("/api/cards/KD-5/messages");
    expect(body.body).toBe("привет");
    expect(body.client_key).toMatch(/^[A-Za-z0-9-]{8,64}$/);
  });

  it("no preview button and no key hint under the field, in a card's thread and an agent's chat (KD-9097)", () => {
    vi.stubGlobal("fetch", vi.fn());
    for (const props of [{ cardId: "KD-5", agents: ["@dev"], assignee: "@dev" }, { chatAgent: "analyst", agents: [] }]) {
      render(Thread, { decisions: {}, canComment: true, ondecision: vi.fn(), ...props });
      expect(screen.queryByRole("button", { name: "Предпросмотр" })).toBeNull();
      expect(document.body.textContent).not.toContain("Enter —");
      expect(document.querySelector(".under")).toBeNull();
      cleanup();
    }
  });

  it("an agent's chat sends to the agent and names it", async () => {
    const fetch = vi.fn(async (_url: string, _init?: RequestInit) => json(msg(1, "text")));
    vi.stubGlobal("fetch", fetch);
    render(Thread, { chatAgent: "analyst", decisions: {}, canComment: true, agents: [], ondecision: vi.fn() });
    expect(screen.getByRole("button", { name: "Прикрепить файлы" })).toBeTruthy(); // files in the chat too
    expect(document.body.textContent).toContain("@analyst");
    const field = screen.getByRole("textbox") as HTMLTextAreaElement;
    await type(field, "как дела?");
    await send(field);
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    const [[url, body]] = posted(fetch);
    expect(url).toBe("/api/agents/analyst/messages");
    expect(body.body).toBe("как дела?");
  });

  it("Enter while composing does not send", async () => {
    const fetch = vi.fn(async (_url: string, _init?: RequestInit) => json(msg(1, "text")));
    vi.stubGlobal("fetch", fetch);
    const { field } = show();
    await type(field, "привет");
    await fireEvent.keyDown(field, { key: "Enter", ctrlKey: true, isComposing: true });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("text typed while sending stays in the field", async () => {
    let answer!: (r: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => (answer = resolve))));
    const { field } = show();
    await type(field, "а");
    await send(field);
    await type(field, "а б");
    answer(json(msg(1, "text")));
    await vi.waitFor(() => expect(field.value).toBe("б"));
  });

  it("a failed send keeps the draft and resends with the same key", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json({ error: "unavailable", message: "down" }, 500))
      .mockResolvedValueOnce(json(msg(1, "text")));
    vi.stubGlobal("fetch", fetch);
    const { field } = show();
    await type(field, "важное");
    await send(field);
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    const button = screen.getByRole("button", { name: "Отправить" }) as HTMLButtonElement;
    await vi.waitFor(() => expect(button.disabled).toBe(false)); // the failed send is over
    expect(field.value).toBe("важное");
    await send(field);
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    const [[, first], [, second]] = posted(fetch);
    expect(second.client_key).toBe(first.client_key);
    await vi.waitFor(() => expect(field.value).toBe(""));
  });

  it("one open question is the default target and the answer goes to it", async () => {
    const fetch = vi.fn(async (_url: string, _init?: RequestInit) => json({}));
    vi.stubGlobal("fetch", fetch);
    const { field, ondecision } = show({ q1: question("q1", "analyst") });
    expect(screen.getByRole("button", { name: /Ответ на вопрос @analyst/, pressed: true })).toBeTruthy();
    await type(field, "показывать всем");
    await send(field);
    await vi.waitFor(() => expect(ondecision).toHaveBeenCalled());
    expect(posted(fetch)).toEqual([["/api/decisions/q1/reply", { text: "показывать всем" }]]);
  });

  it("with two questions nothing is preselected", async () => {
    const fetch = vi.fn(async (_url: string, _init?: RequestInit) => json(msg(1, "text")));
    vi.stubGlobal("fetch", fetch);
    const { field } = show({ q1: question("q1", "analyst"), q2: question("q2", "dev") });
    expect(screen.getByRole("button", { name: /Сообщение для @dev/, pressed: true })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Ответ на вопрос @analyst/, pressed: false })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Ответ на вопрос @dev/, pressed: false })).toBeTruthy();
    await type(field, "общее");
    await send(field);
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    expect(posted(fetch)[0][0]).toBe("/api/cards/KD-5/messages");
  });

  it("a question arriving while the person types does not take the field away", async () => {
    const ondecision = vi.fn();
    const view = render(Thread, { cardId: "KD-5", decisions: {}, canComment: true, agents: ["@dev"], assignee: "@dev", ondecision });
    const field = screen.getByRole("textbox") as HTMLTextAreaElement;
    await type(field, "моё сообщение");
    await view.rerender({ decisions: { q1: question("q1", "dev") } });
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("моё сообщение");
    expect(screen.getByRole("button", { name: /Сообщение для @dev/, pressed: true })).toBeTruthy();
  });

  it("an explicit @mention names the addressee", async () => {
    const { field } = show();
    await type(field, "@boss глянь");
    expect(screen.getByRole("button", { name: /Сообщение для @boss/ })).toBeTruthy();
  });

  it("the draft survives unmount and comes back", async () => {
    const { field } = show();
    await type(field, "черновик");
    cleanup();
    const again = show();
    expect(again.field.value).toBe("черновик");
  });
});

describe("a write's result after the page moved on (KD-9007)", () => {
  afterEach(() => thread.close());

  // card A's message is on its way; the person opens card B (and, with `back`, A again) before it returns
  async function sendAndLeave(back: boolean) {
    let posted!: (r: Response) => void;
    vi.stubGlobal("fetch", vi.fn((_url: string, options?: RequestInit) => options?.method === "POST"
      ? new Promise<Response>((resolve) => (posted = resolve))
      : Promise.resolve(json({ messages: [], watermark: 0 }))));
    await thread.load("KD-5");
    const first = show();
    await type(first.field, "только для KD-5");
    await send(first.field);
    await vi.waitFor(() => expect(posted).toBeTypeOf("function"));
    cleanup();
    thread.close();
    await thread.load("KD-6");
    render(Thread, { cardId: "KD-6", decisions: {}, canComment: true, agents: ["@dev"], ondecision: vi.fn() });
    if (back) {
      cleanup();
      thread.close();
      await thread.load("KD-5");
      show();
    }
    posted(json({ ...msg(999, "text"), body: "только для KD-5" }));
    await new Promise((resolve) => setTimeout(resolve, 0));
  }

  it("a late POST of card A stays out of card B's thread", async () => {
    await sendAndLeave(false);
    expect(thread.card).toBe("KD-6");
    expect(thread.messages).toEqual([]);
  });

  it("A → B → A: the late POST is not inserted, the reopened thread's own read brings it; A's draft is cleared", async () => {
    await sendAndLeave(true);
    expect(thread.card).toBe("KD-5");
    expect(thread.messages).toEqual([]);
    await vi.waitFor(() => expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(""));
  });

  it("A → B → A while the send is on its way: the reopened field resends the same text with the same key", async () => {
    const fetch = vi.fn((_url: string, options?: RequestInit) => options?.method === "POST"
      ? new Promise<Response>(() => undefined)
      : Promise.resolve(json({ messages: [], watermark: 0 })));
    vi.stubGlobal("fetch", fetch);
    const first = show();
    await type(first.field, "один раз");
    await send(first.field);
    cleanup();
    const again = show();
    expect(again.field.value).toBe("один раз");
    await send(again.field);
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    const [[, one], [, two]] = posted(fetch);
    expect(two.client_key).toBe(one.client_key);
  });

  // X goes out and waits; after A → B → A the field is rewritten to Y and sent too; X's success comes back then
  async function olderSucceedsUnderNewer(older: string, newer: string) {
    const posts: { body: Record<string, string>; resolve: (r: Response) => void; reject: (e: Error) => void }[] = [];
    vi.stubGlobal("fetch", vi.fn((_url: string, options?: RequestInit) => options?.method === "POST"
      ? new Promise<Response>((resolve, reject) => posts.push({ body: JSON.parse(String(options.body)), resolve, reject }))
      : Promise.resolve(json({ messages: [], watermark: 0 }))));
    const first = show();
    await type(first.field, older);
    await send(first.field);
    await vi.waitFor(() => expect(posts).toHaveLength(1));
    cleanup();
    const again = show();
    await type(again.field, newer);
    await send(again.field);
    await vi.waitFor(() => expect(posts).toHaveLength(2));
    posts[0].resolve(json({ ...msg(99, "text"), body: older }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    return { posts, field: again.field };
  }

  it("an older send's late success keeps the newer send's key: its retry after a lost response is the same message", async () => {
    const { posts, field } = await olderSucceedsUnderNewer("первое", "второе");
    posts[1].reject(new TypeError("the response was lost after the server saved it"));
    const button = screen.getByRole("button", { name: "Отправить" }) as HTMLButtonElement;
    await vi.waitFor(() => expect(button.disabled).toBe(false));
    expect(field.value).toBe("второе");
    await send(field);
    await vi.waitFor(() => expect(posts).toHaveLength(3));
    expect(posts[2].body.client_key).toBe(posts[1].body.client_key);
  });

  it("an older send's late success leaves the newer send's text alone; the newer one's own success clears it", async () => {
    const { posts, field } = await olderSucceedsUnderNewer("привет", "привет, ещё");
    expect(field.value).toBe("привет, ещё");
    posts[1].resolve(json({ ...msg(100, "text"), body: "привет, ещё" }));
    await vi.waitFor(() => expect(field.value).toBe(""));
  });

  it("a sign-out empties the threads' copy of the drafts", async () => {
    const { field } = show();
    await type(field, "личное");
    cleanup();
    clearDrafts();
    expect(show().field.value).toBe("");
  });

  it("a late upload of card A stays out of card B's thread", async () => {
    let uploaded!: (r: Response) => void;
    vi.stubGlobal("fetch", vi.fn((_url: string, options?: RequestInit) => options?.method === "POST"
      ? new Promise<Response>((resolve) => (uploaded = resolve))
      : Promise.resolve(json({ messages: [], watermark: 0 }))));
    await thread.load("KD-5");
    show();
    const input = document.querySelector("input[type=file]") as HTMLInputElement;
    Object.defineProperty(input, "files", { value: [new File(["x"], "a.txt")] });
    await fireEvent.change(input);
    await fireEvent.click(screen.getByRole("button", { name: "Отправить" }));
    await vi.waitFor(() => expect(uploaded).toBeTypeOf("function"));
    cleanup();
    thread.close();
    await thread.load("KD-6");
    uploaded(json({ ...msg(999, "file"), body: "a.txt" }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(thread.card).toBe("KD-6");
    expect(thread.messages).toEqual([]);
  });
});

describe("thread composer files (KD-9041)", () => {
  const pick = async (...files: File[]) => {
    const input = document.querySelector("input[type=file]") as HTMLInputElement;
    expect(input.multiple).toBe(true);
    Object.defineProperty(input, "files", { value: files, configurable: true });
    await fireEvent.change(input);
  };
  const form = (fetch: ReturnType<typeof vi.fn>, call = 0) => fetch.mock.calls[call][1].body as FormData;

  it("several files and the text go in one request and the form empties", async () => {
    const fetch = vi.fn(async (_url: string, _init?: RequestInit) => json({ ...msg(1, "text"), payload: { files: [] } }));
    vi.stubGlobal("fetch", fetch);
    const { field } = show();
    await type(field, "что тут?");
    await pick(new File(["a"], "a.png", { type: "image/png" }), new File(["bb"], "b.log", { type: "text/plain" }));
    expect(document.body.textContent).toContain("a.png");
    await send(field);
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    expect(fetch.mock.calls[0][0]).toBe("/api/cards/KD-5/files");
    expect(form(fetch).getAll("file").map((f) => (f as File).name)).toEqual(["a.png", "b.log"]);
    expect(form(fetch).get("text")).toBe("что тут?");
    expect(String(form(fetch).get("client_key"))).toMatch(/^[A-Za-z0-9-]{8,64}$/);
    await vi.waitFor(() => expect(field.value).toBe(""));
    expect(document.body.textContent).not.toContain("b.log");
  });

  it("files alone can be sent, in an agent's chat too", async () => {
    const fetch = vi.fn(async (_url: string, _init?: RequestInit) => json(msg(1, "file")));
    vi.stubGlobal("fetch", fetch);
    render(Thread, { chatAgent: "analyst", decisions: {}, canComment: true, agents: [], ondecision: vi.fn() });
    const button = screen.getByRole("button", { name: "Отправить" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    await pick(new File(["a"], "shot.png", { type: "image/png" }));
    expect(button.disabled).toBe(false);
    await fireEvent.click(button);
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    expect(fetch.mock.calls[0][0]).toBe("/api/agents/analyst/files");
    expect(form(fetch).get("text")).toBe("");
  });

  it("a refused send keeps the text and the files in the form with the reason", async () => {
    const fetch = vi.fn(async (_url: string, _init?: RequestInit) => json({ error: "refused", message: "quota", reason: "files_quota" }, 403));
    vi.stubGlobal("fetch", fetch);
    const { field } = show();
    await type(field, "отчёт");
    await pick(new File(["a"], "report.pdf", { type: "application/pdf" }));
    await send(field);
    await vi.waitFor(() => expect(screen.getByRole("alert").textContent).toContain("квоту"));
    expect(field.value).toBe("отчёт");
    expect(document.body.textContent).toContain("report.pdf");
  });

  it("a file over 20 MB is not sent: the form says why", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const { field } = show();
    const big = new File(["x"], "big.mov");
    Object.defineProperty(big, "size", { value: 20 * 1024 * 1024 + 1 });
    await pick(big);
    await send(field);
    await vi.waitFor(() => expect(screen.getByRole("alert").textContent).toContain("big.mov"));
    expect(fetch).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByRole("button", { name: "Убрать «big.mov» из сообщения" }));
    expect(document.body.textContent).not.toContain("big.mov");
  });

  it("a pasted screenshot is attached; a paste with text stays text", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const { field } = show();
    const shot = new File(["png"], "image.png", { type: "image/png" });
    await fireEvent.paste(field, { clipboardData: { types: ["Files"], files: [shot] } });
    expect(document.body.textContent).toMatch(/screenshot-\d{8}-\d{6}\.png/);
    await fireEvent.paste(field, { clipboardData: { types: ["text/plain", "Files"], files: [new File(["x"], "cell.png")] } });
    expect(document.body.textContent).not.toContain("cell.png");
  });

  it("dropped files are attached", async () => {
    vi.stubGlobal("fetch", vi.fn());
    show();
    const composer = document.querySelector(".composer") as HTMLElement;
    const dataTransfer = { types: ["Files"], files: [new File(["a"], "drop.txt", { type: "text/plain" })] };
    await fireEvent.dragOver(composer, { dataTransfer });
    await fireEvent.drop(composer, { dataTransfer });
    expect(document.body.textContent).toContain("drop.txt");
  });
});

describe("thread composer: a voice message (KD-9077)", () => {
  class FakeRecorder {
    static isTypeSupported = (type: string) => type === "audio/webm;codecs=opus";
    state = "inactive";
    mimeType: string;
    ondataavailable: ((event: { data: Blob }) => void) | null = null;
    onstop: (() => void) | null = null;
    onerror: (() => void) | null = null;
    constructor(_stream: MediaStream, options?: MediaRecorderOptions) {
      this.mimeType = options?.mimeType ?? "";
    }
    start() {
      this.state = "recording";
    }
    stop() {
      this.state = "inactive";
      this.ondataavailable?.({ data: new Blob(["opus"], { type: this.mimeType }) });
      this.onstop?.();
    }
  }
  const track = { stop: vi.fn() };
  const microphone = (allowed = true) => {
    vi.stubGlobal("MediaRecorder", FakeRecorder);
    vi.stubGlobal("navigator", {
      ...navigator,
      mediaDevices: {
        getUserMedia: vi.fn(async () => {
          if (!allowed) throw new DOMException("no", "NotAllowedError");
          return { getTracks: () => [track] };
        }),
      },
    });
  };
  const form = (fetch: ReturnType<typeof vi.fn>, call = 0) => fetch.mock.calls[call][1].body as FormData;
  /** Start recording and wait until the microphone is on (the stop buttons work from then). */
  const startRecording = async () => {
    await fireEvent.click(screen.getByRole("button", { name: "Записать голосовое" }));
    await vi.waitFor(() => {
      const stops = screen.getAllByRole("button", { name: "Отправить голосовое" }) as HTMLButtonElement[];
      expect(stops).toHaveLength(2);
      expect(stops.every((b) => !b.disabled)).toBe(true);
    });
  };
  const stopRecording = () => fireEvent.click(screen.getAllByRole("button", { name: "Отправить голосовое" })[0]);

  it("no microphone API, no button", () => {
    vi.stubGlobal("fetch", vi.fn());
    show();
    expect(screen.queryByRole("button", { name: "Записать голосовое" })).toBeNull();
  });

  it("records, shows the time and sends the recording alone in the card's thread", async () => {
    microphone();
    const fetch = vi.fn(async (_url: string, _init?: RequestInit) => json(msg(1, "file")));
    vi.stubGlobal("fetch", fetch);
    const { field } = show();
    await type(field, "черновик остаётся");
    await startRecording();
    expect(screen.getByRole("status").textContent).toContain("из 5:00");
    await stopRecording();
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    expect(fetch.mock.calls[0][0]).toBe("/api/cards/KD-5/files");
    const [file] = form(fetch).getAll("file") as File[];
    expect(file.name).toMatch(/^voice-\d{8}-\d{6}\.webm$/);
    expect(file.type).toBe("audio/webm");
    expect(form(fetch).get("text")).toBeNull(); // no text: it wakes nobody
    expect(field.value).toBe("черновик остаётся");
    expect(track.stop).toHaveBeenCalled();
  });

  it("in an agent's chat too; cancel sends nothing", async () => {
    microphone();
    const fetch = vi.fn(async (_url: string, _init?: RequestInit) => json(msg(1, "file")));
    vi.stubGlobal("fetch", fetch);
    render(Thread, { chatAgent: "analyst", decisions: {}, canComment: true, agents: [], ondecision: vi.fn() });
    await startRecording();
    await fireEvent.click(screen.getByRole("button", { name: "Отменить" }));
    expect(fetch).not.toHaveBeenCalled();
    await startRecording();
    await stopRecording();
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    expect(fetch.mock.calls[0][0]).toBe("/api/agents/analyst/files");
  });

  it("a refused upload keeps the recording in the form; sent again it keeps its key", async () => {
    microphone();
    const fetch = vi.fn(async (_url: string, _init?: RequestInit) => json({ error: "refused", message: "quota", reason: "files_quota" }, 403));
    vi.stubGlobal("fetch", fetch);
    show();
    await startRecording();
    await stopRecording();
    await vi.waitFor(() => expect(screen.getByRole("alert").textContent).toContain("квоту"));
    expect(document.body.textContent).toMatch(/voice-\d{8}-\d{6}\.webm/);
    // the answer may have been lost after the server took it: the same key, the server keeps one message
    await fireEvent.click(screen.getByRole("button", { name: "Отправить" }));
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    expect(form(fetch, 1).get("client_key")).toBe(form(fetch, 0).get("client_key"));
    expect((form(fetch, 1).getAll("file") as File[])[0].name).toMatch(/^voice-/);
  });

  it("a recording that broke off waits in the form", async () => {
    microphone();
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const recorders: FakeRecorder[] = [];
    vi.stubGlobal("MediaRecorder", class extends FakeRecorder {
      constructor(stream: MediaStream, options?: MediaRecorderOptions) {
        super(stream, options);
        recorders.push(this);
      }
    });
    show();
    await startRecording();
    recorders[0].onerror?.();
    await vi.waitFor(() => expect(screen.getByRole("alert").textContent).toContain("запись оборвалась"));
    expect(fetch).not.toHaveBeenCalled();
    expect(document.body.textContent).toMatch(/voice-\d{8}-\d{6}\.webm/);
  });

  it("a refused microphone says so", async () => {
    microphone(false);
    vi.stubGlobal("fetch", vi.fn());
    show();
    await fireEvent.click(screen.getByRole("button", { name: "Записать голосовое" }));
    await vi.waitFor(() => expect(toasts.items.at(-1)?.text).toContain("Нет доступа к микрофону"));
    expect(screen.queryByRole("status")).toBeNull();
  });
});

describe("thread composer and question forms (KD-9008)", () => {
  it("an agent's form question is no target of the field; a link leads to its form", async () => {
    const fetch = vi.fn(async (_url: string, _init?: RequestInit) => json(msg(1, "text")));
    vi.stubGlobal("fetch", fetch);
    const base = question("f1", "analyst");
    const form: DecisionItem = {
      decision: { ...base.decision, payload: FORM },
      view: { text: FORM.text, form: FORM, origin: "agent", form_sha256: "s" },
    };
    const { field } = show({ f1: form });
    expect(screen.queryByRole("button", { name: /Ответ на вопрос/ })).toBeNull();
    const link = screen.getByRole("link", { name: /Ответ на этот вопрос — в его форме/ });
    expect(link.getAttribute("href")).toBe("#decision-f1");
    await type(field, "общее");
    await send(field);
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    expect(posted(fetch)[0][0]).toBe("/api/cards/KD-5/messages");
  });
});

/** A scroller jsdom can measure: its height and scroll position as the test sets them. */
function measured(height: { value: number }) {
  const el = document.querySelector(".thread") as HTMLDivElement;
  let top = 0;
  Object.defineProperty(el, "scrollHeight", { configurable: true, get: () => height.value });
  Object.defineProperty(el, "clientHeight", { configurable: true, get: () => 200 });
  Object.defineProperty(el, "scrollTop", { configurable: true, get: () => top, set: (v: number) => (top = v) });
  return el;
}

describe("tool steps (KD-9036)", () => {
  const call = (seq: number, id: string, tool = "Bash") => ({ ...msg(seq, "tool_call", { tool, id }), body: `${tool}: step ${seq}` });
  const result = (seq: number, id: string, extra: Record<string, unknown> = {}) => ({
    ...msg(seq, "tool_result", { id, ok: true, size: 120, ...extra }),
    body: "",
  });

  it("pairs a call with its result; a result with no call stands alone", () => {
    const steps = toolSteps([call(1, "a"), call(2, "b"), result(3, "a"), result(4, "zz")]);
    expect(steps.map((s) => [s.call?.seq ?? null, s.result?.seq ?? null])).toEqual([[1, 3], [2, null], [null, 4]]);
  });

  async function shown(messages: Message[], props: Record<string, unknown> = {}) {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json({ messages, watermark: 0 })));
    await thread.load("KD-5");
    render(Thread, { cardId: "KD-5", decisions: {}, canComment: true, agents: ["@dev"], assignee: "@dev", ondecision: vi.fn(), ...props });
  }

  it("a call and its result are one line: the tool, what it did, ✓ or ✗ — no raw kind", async () => {
    await shown([call(1, "a"), result(2, "a"), call(3, "b", "Read"), { ...result(4, "b"), payload: { id: "b", ok: false, size: 9 } }]);
    const text = document.body.textContent ?? "";
    expect(text).toContain("Инструменты: 2");
    expect(text).toContain("Bash: step 1");
    expect(text).toContain("✓");
    expect(text).toContain("✗");
    expect(text).not.toContain("tool_result");
    expect(text).not.toContain("tool_call");
  });

  it("the kept output opens in place for whoever controls the agent", async () => {
    await shown([call(1, "a"), result(2, "a", { output: true, session: "s-1" })], { canOutput: true });
    const fetch = vi.fn(async (_url: string) => json({ text: "collected 12 items", size: 18, truncated: false }));
    vi.stubGlobal("fetch", fetch);
    await fireEvent.click(screen.getByRole("button", { name: "Вывод" }));
    await screen.findByText("collected 12 items");
    expect(fetch.mock.calls[0][0]).toBe("/api/sessions/s-1/output/2");
  });

  it("no output button without the right, or without a kept output", async () => {
    await shown([call(1, "a"), result(2, "a", { output: true, session: "s-1" }), call(3, "b"), result(4, "b")]);
    expect(screen.queryByRole("button", { name: "Вывод" })).toBeNull();
    cleanup();
    await shown([call(3, "b"), result(4, "b")], { canOutput: true });
    expect(screen.queryByRole("button", { name: "Вывод" })).toBeNull();
  });

  it("while the agent works: a status line at the bottom and its running step spins", async () => {
    await shown([call(1, "a"), result(2, "a"), call(3, "b")], { working: "@dev" });
    expect(screen.getByRole("status").textContent).toContain("@dev работает");
    expect(document.querySelectorAll(".tool .spin")).toHaveLength(1);
    cleanup();
    await shown([call(1, "a"), result(2, "a"), call(3, "b")]);
    expect(screen.queryByRole("status")).toBeNull();
    expect(document.querySelectorAll(".spin")).toHaveLength(0);
  });
});

describe("a long thread (KD-9009)", () => {
  it("shows earlier messages on request and keeps the reader's place", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValueOnce(json({ messages: [msg(201, "text"), msg(202, "text")], watermark: 0, more: true })),
    );
    await thread.load("KD-5");
    show();
    const height = { value: 1000 };
    const el = measured(height);
    el.scrollTop = 40;
    await fireEvent.scroll(el);
    const fetch = vi.fn(async (_url: string) => {
      height.value = 1600; // the older page renders above what the reader sees
      return json({ messages: [msg(199, "text"), msg(200, "text")], watermark: 0, more: false });
    });
    vi.stubGlobal("fetch", fetch);
    await fireEvent.click(screen.getByRole("button", { name: "Показать ранее" }));
    await vi.waitFor(() => expect(screen.queryByRole("button", { name: "Показать ранее" })).toBeNull());
    expect(fetch.mock.calls[0][0]).toBe("/api/cards/KD-5/messages?before=201&limit=200");
    expect(document.body.textContent).toContain("m199");
    await vi.waitFor(() => expect(el.scrollTop).toBe(640)); // 40 + the 600 px the older page added
  });

  it("counts new messages while the reader is above and goes down on click", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json({ messages: [msg(1, "text")], watermark: 0 })));
    await thread.load("KD-5");
    show();
    const el = measured({ value: 1000 });
    el.scrollTop = 100;
    await fireEvent.scroll(el);
    thread.add(msg(2, "text"), thread.generation);
    thread.add(msg(3, "tool_call"), thread.generation);
    thread.add(msg(4, "text"), thread.generation);
    const chip = await screen.findByRole("button", { name: "Новых сообщений · 2" });
    await fireEvent.click(chip);
    expect(el.scrollTop).toBe(1000);
    await vi.waitFor(() => expect(screen.queryByRole("button", { name: /Новых сообщений/ })).toBeNull());
  });

  it("opens a deep-linked decision older than the window", async () => {
    const pages: Record<string, unknown> = {
      "/api/cards/KD-5/messages?limit=200": { messages: [msg(401, "text")], watermark: 0, more: true },
      "/api/cards/KD-5/messages?before=401&limit=200": { messages: [msg(300, "text")], watermark: 0, more: true },
      "/api/cards/KD-5/messages?before=300&limit=200": {
        messages: [msg(5, "decision", { decision: "q1", state: "open" }), msg(6, "text")],
        watermark: 0,
        more: false,
      },
    };
    const fetch = vi.fn(async (url: string) => json(pages[url] ?? { messages: [], watermark: 0 }));
    vi.stubGlobal("fetch", fetch);
    await thread.load("KD-5");
    render(Thread, {
      cardId: "KD-5",
      decisions: { q1: question("q1", "dev") },
      canComment: true,
      agents: ["@dev"],
      assignee: "@dev",
      highlight: "q1",
      ondecision: vi.fn(),
    });
    await vi.waitFor(() => expect(document.getElementById("decision-q1")).not.toBeNull());
    expect(fetch.mock.calls.map((c) => c[0])).toEqual(Object.keys(pages));
  });

  it("a decision the hunt found stays where it scrolled to", async () => {
    const pages: Record<string, unknown> = {
      "/api/cards/KD-5/messages?limit=200": { messages: [msg(401, "text")], watermark: 0, more: true },
      "/api/cards/KD-5/messages?before=401&limit=200": {
        messages: [msg(5, "decision", { decision: "q1", state: "open" }), msg(6, "text")],
        watermark: 0,
        more: false,
      },
    };
    const height = { value: 1000 };
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      height.value += 600;
      return json(pages[url] ?? { messages: [], watermark: 0 });
    }));
    await thread.load("KD-5");
    let el: HTMLDivElement | undefined;
    const into = vi.fn(() => {
      if (el) el.scrollTop = 7; // where centring the decision put the reader
    });
    Element.prototype.scrollIntoView = into;
    render(Thread, {
      cardId: "KD-5",
      decisions: { q1: question("q1", "dev") },
      canComment: true,
      agents: ["@dev"],
      assignee: "@dev",
      highlight: "q1",
      ondecision: vi.fn(),
    });
    el = measured(height);
    await vi.waitFor(() => expect(into).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(el.scrollTop).toBe(7);
  });

  it("another linked decision on the open card scrolls to it", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json({
      messages: [
        msg(1, "decision", { decision: "q1", state: "open" }),
        msg(2, "text"),
        msg(3, "decision", { decision: "q2", state: "open" }),
      ],
      watermark: 0,
    })));
    await thread.load("KD-5");
    const ids: string[] = [];
    Element.prototype.scrollIntoView = vi.fn(function (this: Element) {
      ids.push(this.id);
    });
    const props = {
      cardId: "KD-5",
      decisions: { q1: question("q1", "dev"), q2: question("q2", "dev") },
      canComment: true,
      agents: ["@dev"],
      assignee: "@dev",
      ondecision: vi.fn(),
    };
    const { rerender } = render(Thread, { ...props, highlight: "q1" });
    await vi.waitFor(() => expect(ids).toEqual(["decision-q1"]));
    await rerender({ ...props, highlight: "q2" });
    await vi.waitFor(() => expect(ids).toEqual(["decision-q1", "decision-q2"]));
  });

  it("the hunt goes past the line where the decision was decided, to where it was opened", async () => {
    const pages: Record<string, unknown> = {
      "/api/cards/KD-5/messages?limit=200": {
        messages: [msg(400, "decision", { decision: "q1", state: "approved" }), msg(401, "text")],
        watermark: 0,
        more: true,
      },
      "/api/cards/KD-5/messages?before=400&limit=200": {
        messages: [msg(5, "decision", { decision: "q1", state: "open" })],
        watermark: 0,
        more: false,
      },
    };
    vi.stubGlobal("fetch", vi.fn(async (url: string) => json(pages[url] ?? { messages: [], watermark: 0 })));
    await thread.load("KD-5");
    render(Thread, {
      cardId: "KD-5",
      decisions: { q1: question("q1", "dev") },
      canComment: true,
      agents: ["@dev"],
      assignee: "@dev",
      highlight: "q1",
      ondecision: vi.fn(),
    });
    await vi.waitFor(() => expect(document.getElementById("decision-q1")).not.toBeNull());
  });

  it("a page that failed ends the hunt with one notice", async () => {
    const fetch = vi.fn(async (url: string) =>
      url.includes("before=")
        ? json({ error: "unavailable", message: "x" }, 500)
        : json({ messages: [msg(401, "text")], watermark: 0, more: true }),
    );
    vi.stubGlobal("fetch", fetch);
    await thread.load("KD-5");
    render(Thread, {
      cardId: "KD-5",
      decisions: { q1: question("q1", "dev") },
      canComment: true,
      agents: ["@dev"],
      assignee: "@dev",
      highlight: "q1",
      ondecision: vi.fn(),
    });
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "Показать ранее" })).toBeTruthy();
  });
});

describe("an answer with files (KD-9082)", () => {
  const pick = async (...files: File[]) => {
    const input = document.querySelector("input[type=file]") as HTMLInputElement;
    Object.defineProperty(input, "files", { value: files, configurable: true });
    await fireEvent.change(input);
  };
  const asking = (expect?: "file"): DecisionItem => {
    const item = question("q1", "dev");
    if (expect) item.decision.payload = { text: "?", expect };
    return { ...item, view: { text: "?", expect: expect ?? "text" } };
  };
  const form = (fetch: ReturnType<typeof vi.fn>, call = 0) => fetch.mock.calls[call][1].body as FormData;

  it("enables attach, paste and drop when replying", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const { field } = show({ q1: asking("file") });
    const attach = screen.getByRole("button", { name: "Прикрепить файлы" }) as HTMLButtonElement;
    expect(attach.disabled).toBe(false);
    await fireEvent.paste(field, { clipboardData: { types: ["Files"], files: [new File(["png"], "image.png", { type: "image/png" })] } });
    expect(document.body.textContent).toMatch(/screenshot-\d{8}-\d{6}\.png/);
    const composer = document.querySelector(".composer") as HTMLElement;
    const dataTransfer = { types: ["Files"], files: [new File(["a"], "drop.txt", { type: "text/plain" })] };
    await fireEvent.dragOver(composer, { dataTransfer });
    await fireEvent.drop(composer, { dataTransfer });
    expect(document.body.textContent).toContain("drop.txt");
  });

  it("reply with files posts multipart to the decision", async () => {
    const fetch = vi.fn(async (_url: string, _init?: RequestInit) => json({ ...asking("file").decision, state: "approved" }));
    vi.stubGlobal("fetch", fetch);
    const { field, ondecision } = show({ q1: asking("file") });
    await pick(new File(["a"], "shot.png", { type: "image/png" }));
    await type(field, "вот");
    await send(field);
    await vi.waitFor(() => expect(ondecision).toHaveBeenCalled());
    expect(fetch.mock.calls[0][0]).toBe("/api/decisions/q1/files");
    expect(form(fetch).getAll("file").map((f) => (f as File).name)).toEqual(["shot.png"]);
    expect(form(fetch).get("text")).toBe("вот");
    expect(String(form(fetch).get("client_key"))).toMatch(/^[A-Za-z0-9-]{8,64}$/);
    await vi.waitFor(() => expect(document.body.textContent).not.toContain("shot.png"));
  });

  it("keeps draft and files when reply fails", async () => {
    const fetch = vi.fn(async (_url: string, _init?: RequestInit) => json({ error: "invalid", message: "x", reason: "text_needed" }, 400));
    vi.stubGlobal("fetch", fetch);
    const { field } = show({ q1: asking() });
    await pick(new File(["a"], "voice.webm", { type: "audio/webm" }));
    await type(field, "ответ");
    await send(field);
    await vi.waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(field.value).toBe("ответ");
    expect(document.body.textContent).toContain("voice.webm");
  });

  it("shows need file for expect=file", () => {
    vi.stubGlobal("fetch", vi.fn());
    show({ q1: asking("file") });
    expect(document.body.textContent).toContain("Нужен файл");
  });

  it("shows need text for a text question; a recording alone is not sent", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const { field } = show({ q1: asking() });
    expect(document.body.textContent).toContain("Нужен текстовый ответ");
    await pick(new File(["a"], "voice.webm", { type: "audio/webm" }));
    await send(field);
    await vi.waitFor(() => expect(screen.getByRole("alert").textContent).toContain("расшифровки нет"));
    expect(fetch).not.toHaveBeenCalled();
  });

  it("voice while replying goes to the draft, not sent", async () => {
    class Rec {
      static isTypeSupported = (type: string) => type === "audio/webm;codecs=opus";
      state = "inactive";
      mimeType = "audio/webm;codecs=opus";
      ondataavailable: ((event: { data: Blob }) => void) | null = null;
      onstop: (() => void) | null = null;
      onerror: (() => void) | null = null;
      start() { this.state = "recording"; }
      stop() {
        this.state = "inactive";
        this.ondataavailable?.({ data: new Blob(["opus"], { type: this.mimeType }) });
        this.onstop?.();
      }
    }
    vi.stubGlobal("MediaRecorder", Rec);
    vi.stubGlobal("navigator", { ...navigator, mediaDevices: { getUserMedia: vi.fn(async () => ({ getTracks: () => [{ stop: vi.fn() }] })) } });
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    show({ q1: asking("file") });
    await fireEvent.click(screen.getByRole("button", { name: "Записать голосовое" }));
    await vi.waitFor(() => {
      const stops = screen.getAllByRole("button", { name: "Отправить голосовое" }) as HTMLButtonElement[];
      expect(stops.every((b) => !b.disabled)).toBe(true);
    });
    await fireEvent.click(screen.getAllByRole("button", { name: "Отправить голосовое" })[0]);
    await vi.waitFor(() => expect(document.body.textContent).toMatch(/\.webm/));
    expect(fetch).not.toHaveBeenCalled();
  });
});
