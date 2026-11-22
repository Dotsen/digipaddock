// the new card's form is the app's — the menu opens it from any page, and it reads its own cards.
import { afterEach, expect, it, vi } from "vitest";
import { newCardForm } from "./newcardform.svelte";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const CARD = { id: "KD-3", n: 3, title: "Цель", project: "shop", status: "To Do", labels: ["ui"] };

afterEach(() => {
  vi.unstubAllGlobals();
  newCardForm.reset();
});

it("show opens the form on the project given and reads the cards for its lists", async () => {
  const fetch = vi.fn(async () => json([CARD]));
  vi.stubGlobal("fetch", fetch);
  newCardForm.show("shop");
  expect([newCardForm.open, newCardForm.project]).toEqual([true, "shop"]);
  await vi.waitFor(() => expect(newCardForm.cards.map((c) => [c.id, c.title, c.labels])).toEqual([["KD-3", "Цель", ["ui"]]]));
  expect(fetch.mock.calls.map((call) => String((call as unknown[])[0]))).toEqual(["/api/cards"]); // every card the person sees
});

it("a failed read keeps the form open with the cards it had", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => json([CARD])));
  newCardForm.show();
  await vi.waitFor(() => expect(newCardForm.cards).toHaveLength(1));
  newCardForm.close();
  expect(newCardForm.open).toBe(false);
  const failing = vi.fn(async () => json({ error: "unknown" }, 500));
  vi.stubGlobal("fetch", failing);
  newCardForm.show("web");
  await vi.waitFor(() => expect(failing).toHaveBeenCalled());
  await Promise.resolve();
  expect([newCardForm.open, newCardForm.project, newCardForm.cards.length]).toEqual([true, "web", 1]);
});

it("a sign-out closes the form and forgets the cards; a read that ends after it is dropped", async () => {
  let release = (_: Response) => {};
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => (release = resolve))));
  newCardForm.show("shop");
  newCardForm.reset();
  release(json([CARD]));
  await new Promise((done) => setTimeout(done, 0));
  expect([newCardForm.open, newCardForm.project, newCardForm.cards]).toEqual([false, "", []]);
});
