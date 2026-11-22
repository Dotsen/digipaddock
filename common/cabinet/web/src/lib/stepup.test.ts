import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import { withStepUp } from "./stepup";
import { assertPasskey } from "./webauthn";

vi.mock("./webauthn", () => ({
  assertPasskey: vi.fn(async (options: { challenge: string }) => ({ id: "cred", over: options.challenge })),
  isCancelled: (exc: unknown) => exc instanceof DOMException && exc.name === "NotAllowedError",
}));

function json(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => vi.unstubAllGlobals());

const needed = () => new ApiError(403, "stepup_required", "confirm");

describe("withStepUp", () => {
  it("runs once without a proof when none is asked for", async () => {
    const run = vi.fn().mockResolvedValueOnce("done");
    expect(await withStepUp("passkey.add", run)).toBe("done");
    expect(run).toHaveBeenCalledWith(undefined);
  });

  it("confirms with a passkey over the challenge of that purpose", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json({ challenge_id: "c1", options: { challenge: "xyz" }, totp: true }));
    vi.stubGlobal("fetch", fetch);
    const run = vi.fn().mockRejectedValueOnce(needed()).mockResolvedValueOnce("ok");
    expect(await withStepUp("password.set", run)).toBe("ok");
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ purpose: "password.set" });
    expect(run).toHaveBeenLastCalledWith({ challenge_id: "c1", credential: { id: "cred", over: "xyz" } });
  });

  it("asks for a TOTP code when there is no passkey", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json({ challenge_id: "c2", options: null, totp: true })));
    const run = vi.fn().mockRejectedValueOnce(needed()).mockResolvedValueOnce("ok");
    const askTotp = vi.fn().mockResolvedValueOnce("123456");
    expect(await withStepUp("totp.setup", run, askTotp)).toBe("ok");
    expect(run).toHaveBeenLastCalledWith({ challenge_id: "c2", totp: "123456" });
  });

  it("gives up when the person declines the TOTP prompt", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json({ challenge_id: "c3", options: null, totp: true })));
    const run = vi.fn().mockRejectedValueOnce(needed());
    await expect(withStepUp("totp.setup", run, async () => null)).rejects.toMatchObject({
      kind: "stepup_required",
    });
    expect(run).toHaveBeenCalledTimes(1);
  });

  it("falls back to a TOTP code when the passkey prompt is dismissed", async () => {
    vi.mocked(assertPasskey).mockRejectedValueOnce(new DOMException("no", "NotAllowedError"));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json({ challenge_id: "c4", options: { challenge: "q" }, totp: true })));
    const run = vi.fn().mockRejectedValueOnce(needed()).mockResolvedValueOnce("ok");
    expect(await withStepUp("passkey.remove", run, async () => "654321")).toBe("ok");
    expect(run).toHaveBeenLastCalledWith({ challenge_id: "c4", totp: "654321" });
  });

  it("passes other errors through untouched", async () => {
    const run = vi.fn().mockRejectedValueOnce(new ApiError(400, "invalid", "short"));
    await expect(withStepUp("password.set", run)).rejects.toMatchObject({ kind: "invalid" });
  });
});
