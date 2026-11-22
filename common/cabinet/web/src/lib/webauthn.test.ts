import { describe, expect, it } from "vitest";
import { fromBase64url, passkeyErrorKey, requestOptionsFromJson, toBase64url } from "./webauthn";

describe("webauthn", () => {
  it("names the browser's passkey errors a person can act on", () => {
    // registering a signing passkey on the authenticator that already holds the sign-in key
    // (excludeCredentials) fails with InvalidStateError — the page said «Something went wrong»
    expect(passkeyErrorKey(new DOMException("x", "NotAllowedError"))).toBe("error.passkey_cancelled");
    expect(passkeyErrorKey(new DOMException("x", "AbortError"))).toBe("error.passkey_cancelled");
    expect(passkeyErrorKey(new DOMException("x", "InvalidStateError"))).toBe("error.passkey_exists");
    expect(passkeyErrorKey(new DOMException("x", "SecurityError"))).toBeNull();
    expect(passkeyErrorKey(new Error("x"))).toBeNull();
    expect(passkeyErrorKey("x")).toBeNull();
  });

  it("round-trips base64url without padding", () => {
    const bytes = new Uint8Array([251, 255, 0, 1, 2, 62, 63]);
    const text = toBase64url(bytes.buffer);
    expect(text).not.toMatch(/[+/=]/);
    expect(new Uint8Array(fromBase64url(text))).toEqual(bytes);
  });

  it("turns request options into binary fields by hand", () => {
    const options = requestOptionsFromJson({
      challenge: "AQID",
      rpId: "cabinet.test",
      allowCredentials: [{ id: "BAUG", type: "public-key", transports: ["internal"] }],
      userVerification: "required",
      timeout: 60000,
    });
    expect(new Uint8Array(options.challenge as ArrayBuffer)).toEqual(new Uint8Array([1, 2, 3]));
    const allowed = options.allowCredentials![0];
    expect(new Uint8Array(allowed.id as ArrayBuffer)).toEqual(new Uint8Array([4, 5, 6]));
    expect(options.rpId).toBe("cabinet.test");
    expect(options.userVerification).toBe("required");
  });
});
