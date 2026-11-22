import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, apiDelete, apiGet, apiPatch, apiPost, apiUpload, setCsrf } from "./api";

function reply(status: number, body: unknown) {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mockFetch(...responses: Response[]) {
  const fn = vi.fn();
  for (const r of responses) fn.mockResolvedValueOnce(r);
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => {
  vi.unstubAllGlobals();
  setCsrf("");
});

describe("api", () => {
  it("reads JSON with the session cookie", async () => {
    const fetch = mockFetch(reply(200, { id: "owner" }));
    expect(await apiGet("/api/me")).toEqual({ id: "owner" });
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe("/api/me");
    expect(init.method).toBe("GET");
    expect(init.credentials).toBe("same-origin");
    expect(new Headers(init.headers).has("X-CSRF-Token")).toBe(false);
  });

  it("sends the CSRF token and a JSON body on writes", async () => {
    const fetch = mockFetch(reply(200, {}), reply(200, {}), reply(200, {}));
    setCsrf("tok");
    await apiPost("/api/cards", { title: "x" });
    await apiPatch("/api/cards/KD-1", { revision: 2 });
    await apiDelete("/api/me/sessions/3");
    for (const [, init] of fetch.mock.calls) {
      expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("tok");
    }
    const [, post] = fetch.mock.calls[0];
    expect(post.method).toBe("POST");
    expect(new Headers(post.headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(post.body)).toEqual({ title: "x" });
    expect(fetch.mock.calls[1][1].method).toBe("PATCH");
    expect(fetch.mock.calls[2][1].method).toBe("DELETE");
  });

  it("uploads a message's files and text as one multipart request with the token", async () => {
    const fetch = mockFetch(reply(200, { id: 4 }));
    setCsrf("tok");
    const a = new File(["hello"], "a.txt", { type: "text/plain" });
    const b = new File(["png"], "b.png", { type: "image/png" });
    expect(await apiUpload("/api/cards/KD-1/files", [a, b], { text: "смотри", client_key: "k-000001" })).toEqual({ id: 4 });
    const [, init] = fetch.mock.calls[0];
    expect(init.body).toBeInstanceOf(FormData);
    const form = init.body as FormData;
    expect(form.getAll("file").map((f) => (f as File).name)).toEqual(["a.txt", "b.png"]);
    expect(form.get("text")).toBe("смотри");
    expect(form.get("client_key")).toBe("k-000001");
    const headers = new Headers(init.headers);
    expect(headers.get("X-CSRF-Token")).toBe("tok");
    expect(headers.has("Content-Type")).toBe(false);
  });

  it("turns an error body into an ApiError of its kind", async () => {
    mockFetch(reply(409, { error: "conflict", message: "revision 3 is stale" }));
    const err = await apiPatch("/api/cards/KD-1", { revision: 3 }).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 409, kind: "conflict", message: "revision 3 is stale" });
  });

  it("gives an unknown kind to a body that is not ours", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(new Response("<html>", { status: 502 })));
    const err = await apiGet("/api/inbox").catch((e) => e);
    expect(err).toMatchObject({ status: 502, kind: "unknown" });
  });

  it("reports a network failure as kind network", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValueOnce(new TypeError("Failed to fetch")));
    const err = await apiGet("/api/inbox").catch((e) => e);
    expect(err).toMatchObject({ status: 0, kind: "network" });
  });

  it("announces a lost session on 401, except while signing in", async () => {
    const seen = vi.fn();
    addEventListener("kd:unauthorized", seen);
    mockFetch(
      reply(401, { error: "unauthorized", message: "sign in" }),
      reply(401, { error: "unauthorized", message: "wrong code" }),
    );
    await expect(apiGet("/api/inbox")).rejects.toMatchObject({ status: 401 });
    expect(seen).toHaveBeenCalledTimes(1);
    await expect(apiPost("/api/auth/code", { user: "a", code: "b" })).rejects.toMatchObject({
      kind: "unauthorized",
    });
    expect(seen).toHaveBeenCalledTimes(1);
    removeEventListener("kd:unauthorized", seen);
  });

  it("answers an empty body with null", async () => {
    mockFetch(new Response(null, { status: 204 }));
    expect(await apiDelete("/api/me/passkeys/2")).toBeNull();
  });
});
