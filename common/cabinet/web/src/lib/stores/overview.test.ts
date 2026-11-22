import { afterEach, describe, expect, it, vi } from "vitest";
import { answer } from "./overview.fixtures";
import { overview } from "./overview.svelte";

function json(body: unknown) {
  return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
}

afterEach(() => vi.unstubAllGlobals());

describe("overview", () => {
  it("asks for the project and period of the address", async () => {
    const fetch = vi.fn(async (_url: string) => json(answer()));
    vi.stubGlobal("fetch", fetch);
    await overview.load({ project: "digipaddock", period: "7d" });
    expect(fetch.mock.calls[0][0]).toBe("/api/dashboard?project=digipaddock&period=7d");
    await overview.load({ project: "", period: "1d" });
    expect(fetch.mock.calls[1][0]).toBe("/api/dashboard?period=1d");
  });

  it("drops the answer of an older request (another project)", async () => {
    let release: (r: Response) => void = () => {};
    const slow = new Promise<Response>((resolve) => (release = resolve));
    const fetch = vi
      .fn()
      .mockReturnValueOnce(slow)
      .mockResolvedValueOnce(json(answer({ watermark: 9 })));
    vi.stubGlobal("fetch", fetch);
    const first = overview.load({ project: "alert", period: "1d" });
    await overview.load({ project: "digipaddock", period: "1d" });
    release(json(answer({ watermark: 3 })));
    await first;
    expect(overview.data?.watermark).toBe(9);
  });

  it("reset drops what was shown and the answer of a read on its way (another person signs in)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(answer({ watermark: 4 }))));
    await overview.load({ project: "", period: "1d" });
    let release: (r: Response) => void = () => {};
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => (release = resolve))));
    const pending = overview.load({ project: "", period: "1d" });
    overview.reset();
    expect(overview.data).toBeNull();
    release(json(answer({ watermark: 5 })));
    await pending;
    expect(overview.data).toBeNull();
  });
});
