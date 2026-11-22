import { describe, expect, it } from "vitest";
import { landing } from "./App.svelte";
import { match } from "./lib/router.svelte";
import type { Me } from "./lib/session.svelte";

const me = (mustSetup: boolean) => ({ id: "owner", must_setup: mustSetup }) as Me;

describe("landing", () => {
  it("sends a stranger to sign in and keeps a deep link", () => {
    expect(landing(null, match("/cards/KD-5"))).toBe("/login?next=%2Fcards%2FKD-5");
    expect(landing(null, match("/"))).toBe("/login");
    expect(landing(null, match("/inbox"))).toBe("/login?next=%2Finbox");
    expect(landing(null, match("/login"))).toBeNull();
  });

  it("offers the setup page right after sign-in only", () => {
    expect(landing(me(true), match("/login?next=/board"))).toBe("/setup");
    expect(landing(me(true), match("/board?project=digipaddock"))).toBeNull();
    expect(landing(me(true), match("/cards/KD-5"))).toBeNull();
  });

  it("goes on to the asked page after sign-in", () => {
    expect(landing(me(false), match("/login?next=/cards/KD-5"))).toBe("/cards/KD-5");
    expect(landing(me(false), match("/login?next=//evil.example"))).toBe("/");
    expect(landing(me(false), match("/login"))).toBe("/"); // the overview is the first screen
    expect(landing(me(false), match("/board"))).toBeNull();
  });

  it("opens an old link's card under the host's current prefix (KD-9184)", () => {
    const moved = { ...me(false), cards: "KD", card_aliases: ["OLD"] } as Me;
    expect(landing(moved, match("/cards/OLD-5?tab=brief"))).toBe("/cards/KD-5?tab=brief");
    expect(landing(moved, match("/cards/KD-5"))).toBeNull();
    expect(landing(me(false), match("/cards/OLD-5"))).toBeNull();
  });
});
