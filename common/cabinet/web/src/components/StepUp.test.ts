import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../lib/api";
import { setLocale } from "../lib/i18n.svelte";
import { confirmed } from "../lib/stepup-dialog.svelte";
import StepUp from "./StepUp.svelte";

vi.mock("../lib/webauthn", () => ({
  assertPasskey: vi.fn(async (options: { challenge: string }) => ({ id: "cred", over: options.challenge })),
  isCancelled: () => false,
}));

function json(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

const needed = () => new ApiError(403, "stepup_required", "confirm");

beforeEach(() => setLocale("ru"));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("StepUp", () => {
  it("asks for a TOTP code and repeats the change with it", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json({ challenge_id: "c1", options: null, totp: true })));
    render(StepUp);
    const run = vi.fn().mockRejectedValueOnce(needed()).mockResolvedValueOnce("ok");
    const done = confirmed("passkey.remove", run);
    const code = await screen.findByLabelText("Код из приложения-аутентификатора");
    await fireEvent.input(code, { target: { value: "123456" } });
    await fireEvent.click(screen.getByRole("button", { name: "Подтвердить" }));
    expect(await done).toBe("ok");
    expect(run).toHaveBeenLastCalledWith({ challenge_id: "c1", totp: "123456" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("confirms with a passkey without a dialog", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(json({ challenge_id: "c2", options: { challenge: "z" }, totp: false }));
    vi.stubGlobal("fetch", fetch);
    render(StepUp);
    const run = vi.fn().mockRejectedValueOnce(needed()).mockResolvedValueOnce("ok");
    expect(await confirmed("roles.set", run)).toBe("ok");
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ purpose: "roles.set" });
    expect(run).toHaveBeenLastCalledWith({ challenge_id: "c2", credential: { id: "cred", over: "z" } });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("leaves the change undone when the dialog is cancelled", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json({ challenge_id: "c3", options: null, totp: true })));
    render(StepUp);
    const run = vi.fn().mockRejectedValueOnce(needed());
    const done = confirmed("users.manage", run);
    await fireEvent.click(await screen.findByRole("button", { name: "Отмена" }));
    await expect(done).rejects.toMatchObject({ kind: "stepup_required" });
    expect(run).toHaveBeenCalledTimes(1);
  });
});
