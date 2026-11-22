import { afterEach, expect, it, vi } from "vitest";
import { changeProviderKey } from "./model-key";
import { apiPost } from "./api";
import { assertPasskey } from "./webauthn";

vi.mock("./api", () => ({ apiPost: vi.fn() }));
vi.mock("./webauthn", () => ({ assertPasskey: vi.fn() }));
afterEach(() => { vi.resetAllMocks(); vi.unstubAllGlobals(); });

it("sends only the digest before signing and the token only with the signature", async () => {
  vi.stubGlobal("crypto", { subtle: { digest: vi.fn(async () => new Uint8Array(32).buffer) } });
  vi.mocked(apiPost).mockResolvedValueOnce({ challenge_id: "nonce", options: { challenge: "root" } })
    .mockResolvedValueOnce({ key: true, audit: true });
  vi.mocked(assertPasskey).mockResolvedValue({ id: "signed" });
  await changeProviderKey("openrouter", "put", "fixture-value");
  const calls = vi.mocked(apiPost).mock.calls;
  expect(calls[0][0]).toBe("/api/models/providers/openrouter/key/challenge");
  expect(calls[0][1]).toMatchObject({ action: "put", digest: expect.stringMatching(/^[a-f0-9]{64}$/) });
  expect(JSON.stringify(calls[0])).not.toContain("fixture-value");
  expect(calls[1]).toEqual(["/api/models/providers/openrouter/key", {
    action: "put", challenge_id: "nonce", credential: { id: "signed" }, key: "fixture-value",
  }]);
});

it("cancelling the passkey sends no key and never substitutes a TOTP code", async () => {
  vi.stubGlobal("crypto", { subtle: { digest: vi.fn(async () => new Uint8Array(32).buffer) } });
  vi.mocked(apiPost).mockResolvedValue({ challenge_id: "nonce", options: {} });
  vi.mocked(assertPasskey).mockRejectedValue(new DOMException("cancel", "NotAllowedError"));
  await expect(changeProviderKey("openrouter", "put", "fixture-value")).rejects.toThrow();
  expect(apiPost).toHaveBeenCalledTimes(1);
  expect(JSON.stringify(vi.mocked(apiPost).mock.calls)).not.toContain("fixture-value");
});
