// The Settings → "Models" section — the list with keys and probes, a new model, enabling and probing.
import { cleanup, fireEvent, render, screen, within } from "@testing-library/svelte";
import { afterEach, expect, it, vi } from "vitest";
import { navigate } from "../lib/router.svelte";
import { session } from "../lib/session.svelte";
import { models } from "../lib/stores/models.svelte";
import type { LlmModel } from "../lib/types";
import SettingsModels from "./SettingsModels.svelte";

afterEach(() => {
  cleanup();
  navigate("/settings/models", { replace: true });
  session.clear();
  models.reset();
  vi.unstubAllGlobals();
});

const login = () =>
  session.signedIn({
    id: "owner", name: "Owner", language: "ru", timezone: "Europe/Lisbon", permissions: { "*": ["settings"] },
    projects: [], csrf: "x", push_public_key: "", second_factor: { passkey: false, totp: false, password: false },
    must_setup: false, memory: false,
  });

const PROVIDERS = [
  { id: "openrouter", title: "OpenRouter", base_url: "https://openrouter.ai/api", hint: "", caching: true, key: false },
  { id: "fireworks", title: "Fireworks", base_url: "https://api.fireworks.ai/inference", hint: "", caching: false, key: false },
  { id: "custom", title: "Custom", base_url: "", hint: "", caching: true, key: true },
];

const orx: LlmModel = {
  id: "or-x", title: "GPT-5 via OpenRouter", provider: "openrouter", upstream: "openai/gpt-5", base_url: "",
  price_in: 1.25, price_out: 10, price_cache_read: 0.125, price_cache_write: 1.25, context: 400000, enabled: 1,
  probe: {}, key: false, experimental: true, created: "2026-11-05T10:00:00Z", updated: "2026-11-05T10:00:00Z",
};

// a model of the subscription — a row of the catalog, not the owner's to change
const opus: LlmModel = {
  id: "claude:opus", title: "Claude Opus", provider: "subscription", upstream: "opus", base_url: "", price_in: 0,
  price_out: 0, price_cache_read: 0, price_cache_write: 0, context: 0, enabled: 1, probe: {}, key: true,
  experimental: false, builtin: true, created: "", updated: "",
};

function serve(list: LlmModel[] = [orx]) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = url === "/api/models/providers" ? PROVIDERS
      : url === "/api/models" && method === "GET" ? list
      : url === "/api/models" && method === "POST" ? { ...orx, ...JSON.parse(String(init!.body)) }
      : url === "/api/models/claude%3Aopus/probe" ? { ...opus, probe: { state: "requested", at: "2026-11-05T10:01:00Z" } }
      : url === "/api/models/or-x/probe" ? { ...orx, probe: { state: "requested", at: "2026-11-05T10:01:00Z" } }
      : url === "/api/models/or-x" && method === "PATCH" ? { ...orx, ...JSON.parse(String(init!.body)) }
      : {};
    return Response.json(body);
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

const sent = (fetch: ReturnType<typeof serve>, method: string) =>
  fetch.mock.calls.filter(([, init]) => init?.method === method).map(([url, init]) => [url, JSON.parse(String(init!.body))]);

it("creates a model: POST /api/models with the form's fields", async () => {
  login();
  const fetch = serve([]);
  render(SettingsModels);
  await fireEvent.click(await screen.findByRole("tab", { name: "Новая модель" }));
  await fireEvent.input(screen.getByLabelText("ID"), { target: { value: "or-x" } });
  await fireEvent.change(screen.getByLabelText("Провайдер"), { target: { value: "openrouter" } });
  await fireEvent.input(screen.getByLabelText("ID модели у провайдера"), { target: { value: "openai/gpt-5" } });
  await fireEvent.input(screen.getByLabelText("Название"), { target: { value: "GPT-5 via OpenRouter" } });
  await fireEvent.input(screen.getByLabelText("Вход, $ за 1M токенов"), { target: { value: "1.25" } });
  await fireEvent.input(screen.getByLabelText("Выход, $ за 1M токенов"), { target: { value: "10" } });
  await fireEvent.input(screen.getByLabelText("Кеш, чтение, $ за 1M токенов"), { target: { value: "0.125" } });
  await fireEvent.input(screen.getByLabelText("Кеш, запись, $ за 1M токенов"), { target: { value: "1.25" } });
  await fireEvent.input(screen.getByLabelText("Контекст, токенов"), { target: { value: "400000" } });
  await fireEvent.click(screen.getByRole("button", { name: "Создать" }));
  await vi.waitFor(() =>
    expect(sent(fetch, "POST")).toEqual([[
      "/api/models",
      { id: "or-x", provider: "openrouter", upstream: "openai/gpt-5", base_url: "", title: "GPT-5 via OpenRouter",
        price_in: 1.25, price_out: 10, price_cache_read: 0.125, price_cache_write: 1.25, context: 400000 },
    ]]),
  );
});

it("the kind's cache is a sentence about the cache prices, never a bare boolean (stage 05.11)", async () => {
  login();
  serve([]);
  render(SettingsModels);
  await fireEvent.click(await screen.findByRole("tab", { name: "Новая модель" }));
  expect(await screen.findByText(/Кеш подсказок у провайдера есть/)).toBeTruthy();
  await fireEvent.change(screen.getByLabelText("Провайдер"), { target: { value: "fireworks" } });
  expect(await screen.findByText(/Кеша подсказок у провайдера нет/)).toBeTruthy();
  expect(screen.queryByText("true")).toBeNull();
  expect(screen.queryByText("false")).toBeNull();
});

it("a model whose kind has no key on the host says how root puts it there", async () => {
  login();
  serve();
  render(SettingsModels);
  expect(await screen.findByText(/kd-secret put llm-openrouter/)).toBeTruthy();
  expect(screen.getByText("ключа на хосте нет")).toBeTruthy();
});

it("«Проверить» asks the host through POST /api/models/{id}/probe and the row says so", async () => {
  login();
  const fetch = serve();
  render(SettingsModels);
  await fireEvent.click(await screen.findByRole("button", { name: "Проверить" }));
  await vi.waitFor(() => expect(sent(fetch, "POST")).toEqual([["/api/models/or-x/probe", {}]]));
  expect(await screen.findByText("проверка запрошена…")).toBeTruthy();
});

it("a model that is not a Claude is marked experimental; a probe's outcome is shown", async () => {
  login();
  serve([{ ...orx, probe: { at: "2026-11-05T10:02:00Z", ok: true, model: "openai/gpt-5", ms: 1200, detail: "OK" } }]);
  render(SettingsModels);
  expect(await screen.findByText("экспериментально")).toBeTruthy();
  expect(screen.getByText("ответила openai/gpt-5 за 1200 мс")).toBeTruthy();
});

it("a failed probe shows its detail; the switch disables the model with PATCH {enabled: 0}", async () => {
  login();
  const fetch = serve([{ ...orx, probe: { at: "2026-11-05T10:02:00Z", ok: false, model: "", ms: 300, detail: "exit 1: 401 Unauthorized" } }]);
  render(SettingsModels);
  expect(await screen.findByText("не ответила: exit 1: 401 Unauthorized")).toBeTruthy();
  await fireEvent.click(screen.getByLabelText("Включена"));
  await vi.waitFor(() => expect(sent(fetch, "PATCH")).toEqual([["/api/models/or-x", { enabled: 0 }]]));
});

it("three tabs: the models, the providers' keys for the owner, a new model; the tab is in the address (KD-9222)", async () => {
  login();
  PROVIDERS.forEach((p) => Object.assign(p, { manage_key: true }));
  try {
    serve();
    render(SettingsModels);
    expect(await screen.findByText("GPT-5 via OpenRouter")).toBeTruthy();
    expect(screen.getAllByRole("tab").map((b) => b.textContent)).toEqual(["Модели", "Ключи провайдеров", "Новая модель"]);
    expect(screen.queryByLabelText("Ключ · OpenRouter")).toBeNull();
    // a model without its key links to the keys, not to a root command
    expect(screen.queryByText(/kd-secret put/)).toBeNull();
    await fireEvent.click(screen.getByRole("link", { name: "Добавить ключ" }));
    expect(await screen.findByLabelText("Ключ · OpenRouter")).toBeTruthy();
    expect(location.search).toBe("?tab=keys");
    expect(screen.queryByText("GPT-5 via OpenRouter")).toBeNull();
    await fireEvent.click(screen.getByRole("tab", { name: "Новая модель" }));
    expect(await screen.findByLabelText("ID модели у провайдера")).toBeTruthy();
    expect(location.search).toBe("?tab=add");
  } finally {
    PROVIDERS.forEach((p) => delete (p as { manage_key?: boolean }).manage_key);
  }
});

it("no keys tab for someone who may not change the keys", async () => {
  login();
  serve();
  render(SettingsModels);
  expect(await screen.findByText("GPT-5 via OpenRouter")).toBeTruthy();
  expect(screen.getAllByRole("tab").map((b) => b.textContent)).toEqual(["Модели", "Новая модель"]);
});

it("a model of the subscription is listed first: probed like any, never changed, switched off or deleted (KD-9266)", async () => {
  login();
  const fetch = serve([opus, orx]);
  render(SettingsModels);
  expect(await screen.findByText("Claude Opus")).toBeTruthy();
  const rows = screen.getAllByRole("listitem");
  expect(rows.map((row) => row.querySelector("b")?.textContent)).toEqual(["Claude Opus", "GPT-5 via OpenRouter"]);
  const builtin = within(rows[0]);
  expect(builtin.getByText(/на подписке Claude/)).toBeTruthy();
  expect(builtin.queryByRole("button", { name: "Править" })).toBeNull();
  expect(builtin.queryByRole("button", { name: "Удалить" })).toBeNull();
  expect(builtin.queryByLabelText("Включена")).toBeNull();
  expect(builtin.queryByText("экспериментально")).toBeNull();
  expect(builtin.queryByText(/ключ/)).toBeNull();
  expect(within(rows[1]).getByRole("button", { name: "Удалить" })).toBeTruthy();
  await fireEvent.click(builtin.getByRole("button", { name: "Проверить" }));
  await vi.waitFor(() => expect(sent(fetch, "POST")).toEqual([["/api/models/claude%3Aopus/probe", {}]]));
  expect(await builtin.findByText("проверка запрошена…")).toBeTruthy();
});

it("a subscription's model on a host without the subscription's token says so and is not probed (KD-9266)", async () => {
  login();
  serve([{ ...opus, key: false }]);
  render(SettingsModels);
  expect(await screen.findByText("токена подписки на хосте нет")).toBeTruthy();
  expect((screen.getByRole("button", { name: "Проверить" }) as HTMLButtonElement).disabled).toBe(true);
});

it("a model added while the list is shown goes after the subscription's, by its id (KD-9266)", () => {
  models.list = [opus, orx];
  models.put({ ...orx, id: "a-first", title: "A" });
  models.put({ ...opus, probe: { state: "requested", at: "2026-11-05T10:01:00Z" } });
  expect(models.list.map((m) => m.id)).toEqual(["claude:opus", "a-first", "or-x"]);
  expect(models.list[0].probe?.state).toBe("requested");
  expect([models.name("claude:opus"), models.name("or-x"), models.name("gone")]).toEqual(["Claude Opus · подписка", "GPT-5 via OpenRouter", "gone"]);
});
