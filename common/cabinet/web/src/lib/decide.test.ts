import { afterEach, describe, expect, it, vi } from "vitest";
import { approveDecisions, NoPasskey, shownRevisions, withPasskey, withTotp } from "./decide";
import type { DecisionItem } from "./types";

vi.mock("./webauthn", () => ({
  assertPasskey: vi.fn(async (options: { challenge: string }) => ({ id: "cred", over: options.challenge })),
}));

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const challenge = (n: number, over = {}) => ({
  challenge_id: `c${n}`,
  options: { challenge: `v${n}` },
  step_up: true,
  totp: true,
  expires: "2026-10-15T12:05:00Z",
  revisions: { "KD-1": 3 },
  ...over,
});

const bodies = (fetch: ReturnType<typeof vi.fn>) =>
  fetch.mock.calls.map(([url, init]) => [url, JSON.parse(init.body)]);

/** The decisions as a page shows them: over KD-1 at the revision given (none: a view without its card). */
const shown = (ids: string[], revision: number | null = 3): DecisionItem[] =>
  ids.map(
    (id) =>
      ({
        decision: { id, card: "KD-1" },
        view: revision === null ? {} : { card: { id: "KD-1", revision } },
      }) as unknown as DecisionItem,
  );

afterEach(() => vi.unstubAllGlobals());

describe("approveDecisions", () => {
  it("signs the challenge with a passkey and sends the term of the grant", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json(challenge(1)))
      .mockResolvedValueOnce(json({ decisions: [{ id: "d1", state: "approved" }] }));
    vi.stubGlobal("fetch", fetch);
    const result = await approveDecisions(shown(["d1", "d2"]), withPasskey, 72);
    expect(result.decisions[0].state).toBe("approved");
    expect(bodies(fetch)).toEqual([
      ["/api/decisions/challenge", { ids: ["d1", "d2"] }],
      ["/api/decisions/approve", { challenge_id: "c1", credential: { id: "cred", over: "v1" }, ttl_hours: 72 }],
    ]);
  });

  it("approves a decision without step-up with no proof at all", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json(challenge(1, { step_up: false, options: null, totp: false })))
      .mockResolvedValueOnce(json({ decisions: [] }));
    vi.stubGlobal("fetch", fetch);
    await approveDecisions(shown(["d1"]), withPasskey);
    expect(bodies(fetch)[1]).toEqual(["/api/decisions/approve", { challenge_id: "c1" }]);
  });

  it("asks a new challenge once when the old one expired", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json(challenge(1)))
      .mockResolvedValueOnce(json({ error: "expired", message: "ask again" }, 410))
      .mockResolvedValueOnce(json(challenge(2)))
      .mockResolvedValueOnce(json({ decisions: [] }));
    vi.stubGlobal("fetch", fetch);
    await approveDecisions(shown(["d1"]), withTotp("123456"));
    expect(bodies(fetch).map(([url, body]) => [url, body.challenge_id ?? null, body.totp ?? null])).toEqual([
      ["/api/decisions/challenge", null, null],
      ["/api/decisions/approve", "c1", "123456"],
      ["/api/decisions/challenge", null, null],
      ["/api/decisions/approve", "c2", "123456"],
    ]);
  });

  it("gives up after the second expiry and passes conflicts through", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(json(challenge(1)))
        .mockResolvedValueOnce(json({ error: "expired" }, 410))
        .mockResolvedValueOnce(json(challenge(2)))
        .mockResolvedValueOnce(json({ error: "expired" }, 410)),
    );
    await expect(approveDecisions(shown(["d1"]), withTotp("1"))).rejects.toMatchObject({ status: 410 });
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(json(challenge(1)))
        .mockResolvedValueOnce(json({ error: "conflict", message: "KD-1 changed" }, 409)),
    );
    await expect(approveDecisions(shown(["d1"]), withTotp("1"))).rejects.toMatchObject({ status: 409 });
  });

  it("refuses to sign a challenge over a card revision other than the one shown — before any prompt", async () => {
    // global review 15.10.2026: the page read KD-1 at revision 3; the server would bind the signature to 4
    const fetch = vi.fn().mockResolvedValueOnce(json(challenge(1, { revisions: { "KD-1": 4 } })));
    vi.stubGlobal("fetch", fetch);
    const { assertPasskey } = await import("./webauthn");
    (assertPasskey as unknown as ReturnType<typeof vi.fn>).mockClear();
    await expect(approveDecisions(shown(["d1"]), withPasskey)).rejects.toMatchObject({ status: 409 });
    expect(assertPasskey).not.toHaveBeenCalled();
    expect(fetch).toHaveBeenCalledTimes(1); // no approve
    // a view without its card binds nothing: the server's revision check stands alone
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(json(challenge(2, { revisions: { "KD-1": 4 } })))
        .mockResolvedValueOnce(json({ decisions: [] })),
    );
    await approveDecisions(shown(["d1"], null), withTotp("1"));
    expect(shownRevisions(shown(["d1", "d2"]))).toEqual({ "KD-1": 3 });
  });

  it("refuses a component plan when the deployed tree moved since the page was read (KD-9108)", async () => {
    const plan = [{ decision: { id: "d1", card: "KD-1" }, view: { card: { id: "KD-1", revision: 3 }, deployed: "aaa1111" } }] as unknown as DecisionItem[];
    expect(shownRevisions(plan)).toEqual({ "KD-1": 3, "deployed:KD-1": "aaa1111" });
    const fetch = vi.fn().mockResolvedValueOnce(json(challenge(1, { revisions: { "KD-1": 3, "deployed:KD-1": "bbb2222" } })));
    vi.stubGlobal("fetch", fetch);
    await expect(approveDecisions(plan, withTotp("1"))).rejects.toMatchObject({ status: 409 });
    expect(fetch).toHaveBeenCalledTimes(1); // no approve
  });

  it("signing a mode sends it in the challenge", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json(challenge(1, { totp: false, mode: "merge" })))
      .mockResolvedValueOnce(json({ decisions: [] }));
    vi.stubGlobal("fetch", fetch);
    await approveDecisions(shown(["d1"]), withPasskey, undefined, "merge");
    expect(bodies(fetch)).toEqual([
      ["/api/decisions/challenge", { ids: ["d1"], mode: "merge" }],
      ["/api/decisions/approve", { challenge_id: "c1", credential: { id: "cred", over: "v1" } }],
    ]);
  });

  it("says when there is no passkey to sign with, and whether TOTP is there", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json(challenge(1, { options: null }))));
    const err = await approveDecisions(shown(["d1"]), withPasskey).catch((e) => e);
    expect(err).toBeInstanceOf(NoPasskey);
    expect(err.totp).toBe(true);
  });
});
