import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
vi.mock("../chats.svelte", async original => ({...(await original<object>()), refreshChats: vi.fn()}));
import { setLocale } from "../i18n.svelte";
import { resyncAll } from "../live.svelte";
import type { BriefState, BriefView } from "../types";
import { brief, briefLabel, briefTone } from "./brief.svelte";

function state(over: Partial<BriefState> = {}): BriefState {
  return { state: "ready", version: 1, approved: false, legacy: false, questions: 0, error: "", ...over };
}

function view(over: Partial<BriefState> = {}): BriefView {
  return { brief: state(over), versions: [], current: { description: "d", acceptance: [] }, changes: [] };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

beforeEach(() => setLocale("ru"));
afterEach(() => {
  brief.close();
  vi.unstubAllGlobals();
});

describe("brief store", () => {
  it("loads the card's brief", async () => {
    const fetch = vi.fn(async (_url: string) => json(view()));
    vi.stubGlobal("fetch", fetch);
    await brief.load("KD-5");
    expect(fetch.mock.calls[0][0]).toBe("/api/cards/KD-5/brief");
    expect(brief.view?.brief.version).toBe(1);
  });

  it("late answer of an older card is dropped", async () => {
    let answer!: (r: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => (answer = resolve))));
    const older = brief.load("KD-5");
    const first = answer;
    vi.stubGlobal("fetch", vi.fn(async () => json(view({ version: 7 }))));
    const newer = brief.load("KD-6");
    first(json(view({ version: 1 })));
    await Promise.all([older, newer]);
    expect(brief.id).toBe("KD-6");
    expect(brief.view?.brief.version).toBe(7);
  });

  it("an event of the card reloads, of another card does not", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(view())));
    await brief.load("KD-5");
    const fetch = vi.fn(async () => json(view({ version: 2 })));
    vi.stubGlobal("fetch", fetch);
    await brief.onEvent({ seq: 1, kind: "card.changed", card: "KD-9", payload: {} });
    await brief.onEvent({ seq: 2, kind: "message.added", card: "KD-5", payload: { via: "card.changed" } }); // an echo
    expect(fetch).not.toHaveBeenCalled();
    await brief.onEvent({ seq: 3, kind: "brief.version", card: "KD-5", payload: { version: 2 } });
    expect(brief.view?.brief.version).toBe(2);
  });

  it("a failed load keeps the last view and retries", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(view())));
    await brief.load("KD-5");
    vi.stubGlobal("fetch", vi.fn(async () => json({ error: "unavailable", message: "down" }, 503)));
    await brief.onEvent({ seq: 1, kind: "card.changed", card: "KD-5", payload: {} });
    expect(brief.view?.brief.version).toBe(1);
    expect(brief.stale).toBe(true);
    vi.stubGlobal("fetch", vi.fn(async () => json(view({ version: 3 }))));
    resyncAll();
    await tick();
    expect(brief.view?.brief.version).toBe(3);
    expect(brief.stale).toBe(false);
  });

  it("a closed store reads nothing", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(view())));
    await brief.load("KD-5");
    brief.close();
    const fetch = vi.fn(async () => json(view()));
    vi.stubGlobal("fetch", fetch);
    await brief.onEvent({ seq: 1, kind: "card.changed", card: "KD-5", payload: {} });
    resyncAll();
    await tick();
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("briefLabel", () => {
  it("names every state in words", () => {
    expect(briefLabel(state({ state: "not_requested", version: 0 }))).toBe("Не запрошено");
    expect(briefLabel(state({ state: "queued", version: 0 }))).toBe("В очереди");
    expect(briefLabel(state({ state: "in_progress", version: 0 }))).toBe("Готовится");
    expect(briefLabel(state({ state: "waiting_user", questions: 2 }))).toBe("Ждёт ваших ответов · 2");
    expect(briefLabel(state())).toBe("Готово · v1");
    expect(briefLabel(state({ approved: true, version: 2 }))).toBe("Согласовано · v2");
    expect(briefLabel(state({ legacy: true, version: 0 }))).toBe("Существующее описание · требуется подтверждение");
    expect(briefLabel(state({ state: "stale" }))).toBe("Изменено после готовности · v1");
    expect(briefLabel(state({ state: "stale", approved: true }))).toBe("Изменено после согласования");
    expect(briefLabel(state({ state: "failed" }))).toBe("Ошибка подготовки");
  });

  it("a brief its council reads says so, with the version (KD-9267)", () => {
    expect(briefLabel(state({ state: "in_council", version: 2 }))).toBe("ТЗ ред. 2 читает совет");
    expect(briefTone(state({ state: "in_council", version: 2 }))).toBe("info");
  });
});
