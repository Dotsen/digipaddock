import { cleanup, fireEvent, render, screen, within } from "@testing-library/svelte";
import { afterEach, expect, it, vi } from "vitest";
import { session } from "../lib/session.svelte";
import { models } from "../lib/stores/models.svelte";
import { projects } from "../lib/stores/projects.svelte";
import SettingsProjects from "./SettingsProjects.svelte";

afterEach(() => {
  cleanup();
  session.clear();
  projects.reset();
  models.reset();
  vi.unstubAllGlobals();
});

const login = (memory = true) =>
  session.signedIn({
    id: "owner", name: "Owner", language: "ru", timezone: "Europe/Lisbon", permissions: { "*": ["settings"] },
    projects: [], csrf: "x", push_public_key: "", second_factor: { passkey: false, totp: false, password: false },
    must_setup: false, memory,
  });

const shop = { id: "shop", name: "Shop", route: "@dev", repo: "", repo_state: "", memory_bank: "", done_by: "", model: "", effort: "" };
const orx = { id: "or-x", title: "GPT-5 via OpenRouter", provider: "openrouter", enabled: 1, key: true, experimental: true };

function serve() {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const body = url === "/api/projects/routes" ? ["@owner", "@dev"]
      : url === "/api/models" ? [orx]
      : url === "/api/projects" ? [shop]
      : url === "/api/projects/shop" && init?.method === "PATCH" ? { ...shop, ...JSON.parse(String(init.body)) }
      : [];
    return Response.json(body);
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

const patches = (fetch: ReturnType<typeof serve>) =>
  fetch.mock.calls.filter(([, init]) => init?.method === "PATCH").map(([, init]) => JSON.parse(String(init!.body)));

it("binds a project's memory bank and sends only what changed (KD-9183)", async () => {
  login();
  const fetch = serve();
  render(SettingsProjects);
  await fireEvent.click(await screen.findByRole("button", { name: "Править" }));
  const bank = screen.getByLabelText("Банк памяти Hindsight") as HTMLInputElement;
  await fireEvent.input(bank, { target: { value: " coding-agent::_Shop.v2 " } });
  await fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
  await vi.waitFor(() => expect(patches(fetch)).toEqual([{ memory_bank: "coding-agent::_Shop.v2" }]));
});

it("names the agent that closes the project's cards Done, the owner's acceptance by default (KD-9184)", async () => {
  login();
  const fetch = serve();
  render(SettingsProjects);
  await fireEvent.click(await screen.findByRole("button", { name: "Править" }));
  const closer = screen.getByLabelText("Кто закрывает Done") as HTMLSelectElement;
  expect([...closer.options].map((o) => [o.value, o.text])).toEqual([["", "Владелец — приёмка"], ["dev", "@dev"]]);
  await fireEvent.change(closer, { target: { value: "dev" } });
  await fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
  await vi.waitFor(() => expect(patches(fetch)).toEqual([{ done_by: "dev" }]));
});

it("on a host without Hindsight a project has no memory bank to bind (KD-9192)", async () => {
  login(false);
  const fetch = serve();
  render(SettingsProjects);
  await fireEvent.click(await screen.findByRole("button", { name: "Править" }));
  expect(screen.queryByLabelText("Банк памяти Hindsight")).toBeNull();
  await fireEvent.change(screen.getByLabelText("Кто закрывает Done"), { target: { value: "dev" } });
  await fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
  await vi.waitFor(() => expect(patches(fetch)).toEqual([{ done_by: "dev" }]));
});

it("sets the project's default model and effort (KD-9217)", async () => {
  login();
  const fetch = serve();
  render(SettingsProjects);
  await fireEvent.click(await screen.findByRole("button", { name: "Править" }));
  const model = screen.getByLabelText("Модель") as HTMLSelectElement;
  await vi.waitFor(() => expect([...model.options].map((o) => [o.value, o.text])).toEqual([["", "подписка Claude"], ["or-x", "GPT-5 via OpenRouter"]]));
  await fireEvent.change(model, { target: { value: "or-x" } });
  const effort = screen.getByLabelText("Усилие") as HTMLSelectElement;
  expect([...effort.options].map((o) => o.value)).toEqual(["", "low", "medium", "high", "xhigh", "max"]);
  await fireEvent.change(effort, { target: { value: "low" } });
  await fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
  await vi.waitFor(() => expect(patches(fetch)).toEqual([{ model: "or-x", effort: "low" }]));
});

it("refuses a bank the host would not take before any request", async () => {
  login();
  const fetch = serve();
  render(SettingsProjects);
  await fireEvent.click(await screen.findByRole("button", { name: "Править" }));
  await fireEvent.input(screen.getByLabelText("Банк памяти Hindsight"), { target: { value: "a/b" } });
  expect(screen.getByText(/не начинается с «.», «-» или «:»/)).toBeTruthy();
  expect((screen.getByRole("button", { name: "Сохранить" }) as HTMLButtonElement).disabled).toBe(true);
  expect(patches(fetch)).toEqual([]);
});

it("sets the default council of the project's cards and sends it only when changed (KD-9266)", async () => {
  login();
  const fetch = serve();
  render(SettingsProjects);
  await fireEvent.click(await screen.findByRole("button", { name: "Править" }));
  const code = within(screen.getByRole("group", { name: "код" }));
  await fireEvent.focus(code.getByRole("combobox", { name: "Критики" }));
  await fireEvent.click(await code.findByRole("option", { name: /or-x|OpenRouter/ }));
  await fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
  await vi.waitFor(() => expect(patches(fetch)).toEqual([{ council: { code: { critics: ["or-x"], rounds: 3, min_lines: 100 } } }]));
});
