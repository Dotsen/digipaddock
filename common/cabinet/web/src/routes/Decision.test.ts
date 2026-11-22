import { cleanup, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import { router, startRouter } from "../lib/router.svelte";
import Decision from "./Decision.svelte";

const PROPOSAL = "ab".repeat(16);
const ID = "cd".repeat(16);

function answer(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("Decision", () => {
  beforeEach(() => {
    setLocale("ru");
    history.replaceState(null, "", `/decisions/by-proposal/${PROPOSAL}`);
    startRouter();
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("finds an old BOSS link's decision by its proposal and opens its card", async () => {
    const fetch = vi.fn(async () => answer([{ decision: { id: ID, card: "KD-7" }, view: {} }]));
    vi.stubGlobal("fetch", fetch);
    render(Decision, { proposal: PROPOSAL });
    await vi.waitFor(() => expect(router.current.path).toBe(`/cards/KD-7?decision=${ID}`));
    expect(String((fetch.mock.calls[0] as unknown[])[0])).toBe(`/api/decisions?state=all&proposal=${PROPOSAL}`);
  });

  it("says not found when no decision has that proposal", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => answer([])));
    render(Decision, { proposal: PROPOSAL });
    expect(await screen.findByRole("heading", { name: "Не найдено" })).toBeTruthy();
  });
});
