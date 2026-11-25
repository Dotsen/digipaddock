// The menu's search: a card's number opens that card, anything else the board searched.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cardNumber, searchTarget } from "./findcard";

function cards(...ids: string[]) {
  const list = ids.map((id) => ({ id, n: Number(id.split("-")[1]), title: id, labels: [], servers: [] }));
  return vi.fn(async () => new Response(JSON.stringify(list), { headers: { "Content-Type": "application/json" } }));
}

afterEach(() => vi.unstubAllGlobals());

describe("cardNumber", () => {
  it("reads a full key in any case, a bare number and #number", () => {
    expect(cardNumber("KD-9443")).toEqual({ n: 9443, key: "KD-9443" });
    expect(cardNumber(" kd-9443 ")).toEqual({ n: 9443, key: "KD-9443" });
    expect(cardNumber("9443")).toEqual({ n: 9443, key: "" });
    expect(cardNumber("#12")).toEqual({ n: 12, key: "" });
  });

  it("is null for words, a number among words, zero or a number too long for a card", () => {
    for (const text of ["", "deploy", "fix 12", "0", "0123", "1234567", "KD-", "-12"]) expect(cardNumber(text)).toBeNull();
  });
});

describe("searchTarget", () => {
  it("leads words to the board searched, without asking anything", async () => {
    const fetch = cards();
    vi.stubGlobal("fetch", fetch);
    expect(await searchTarget("голос бота")).toBe("/board?q=%D0%B3%D0%BE%D0%BB%D0%BE%D1%81%20%D0%B1%D0%BE%D1%82%D0%B0");
    expect(await searchTarget("  ")).toBe("/board");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("opens the card a number names when the search finds one card of that number", async () => {
    // the search puts the card itself first, then the cards that mention its number
    const fetch = cards("KD-9443", "KD-9500", "KD-9501");
    vi.stubGlobal("fetch", fetch);
    expect(await searchTarget("9443")).toBe("/cards/KD-9443");
    expect(await searchTarget("kd-9443")).toBe("/cards/KD-9443");
    expect(await searchTarget("#9443")).toBe("/cards/KD-9443");
    expect(String((fetch.mock.calls[0] as unknown[])[0])).toBe("/api/cards?q=9443");
  });

  it("leads to the board when no card or more than one has that number, or the search failed", async () => {
    vi.stubGlobal("fetch", cards("KD-9500"));
    expect(await searchTarget("9443")).toBe("/board?q=9443");
    vi.stubGlobal("fetch", cards("KD-12", "OLD-12"));
    expect(await searchTarget("12")).toBe("/board?q=12");
    vi.stubGlobal("fetch", cards("KD-12"));
    expect(await searchTarget("AB-12")).toBe("/board?q=AB-12");  // another prefix: not this card
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("offline"); }));
    expect(await searchTarget("12")).toBe("/board?q=12");
  });
});
