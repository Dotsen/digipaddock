// a key saved before root enrolled a signing passkey says who does what, not «add a passkey or TOTP».
import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, expect, it, vi } from "vitest";
import { session } from "../lib/session.svelte";
import { toasts } from "../lib/toast.svelte";
import ProviderKey from "./ProviderKey.svelte";

afterEach(() => {
  cleanup();
  session.clear();
  toasts.items = [];
  vi.unstubAllGlobals();
});

it("a save before root's enrollment names the enrollment command", async () => {
  session.signedIn({
    id: "owner", name: "Owner", language: "ru", timezone: "Europe/Lisbon", permissions: { "*": ["settings"] },
    projects: [], csrf: "x", push_public_key: "", second_factor: { passkey: true, totp: false, password: false },
    must_setup: false, memory: false,
  });
  vi.stubGlobal("fetch", vi.fn(async () =>
    Response.json({ error: "setup_required", message: "root must enroll an owner signing key with kd-model-keys" }, { status: 403 })));
  const changed = vi.fn(async () => {});
  render(ProviderKey, {
    provider: { id: "openrouter", title: "OpenRouter", base_url: "", hint: "", caching: true, key: false, manage_key: true },
    changed,
  });
  await fireEvent.input(screen.getByLabelText("Ключ · OpenRouter"), { target: { value: "sk-test" } });
  await fireEvent.click(screen.getByRole("button", { name: "Сохранить ключ с подписью" }));
  await vi.waitFor(() => expect(toasts.items.map((x) => x.text)).toEqual([
    "Подпись ключей ещё не включена: root должен зарегистрировать ваш passkey — kd-model-keys enroll owner",
  ]));
  expect((screen.getByLabelText("Ключ · OpenRouter") as HTMLInputElement).value).toBe("");
  expect(changed).not.toHaveBeenCalled();
});
