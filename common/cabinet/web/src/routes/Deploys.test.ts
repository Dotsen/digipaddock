// «Деплой»: a panel per project with a row per environment — its status, what it runs, how far it is ahead of prod
// and what waits; a project whose read failed says so in words; the owner's launch asks for the passkey alone.
import { cleanup, fireEvent, render, screen, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import type { DeployEnv, DeployList, DeployRun } from "../lib/types";
import Deploys from "./Deploys.svelte";

function json(body: unknown) {
  return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
}

const H = "26711f9" + "0".repeat(33);
const P = "11efc6c" + "0".repeat(33);

function run(env: string, status: DeployRun["status"], sha: string): DeployRun {
  return {
    project: "alert", env, run: 1, number: 1, workflow: `deploy-${env}.yml`, status, event: "push", sha, ref: "master",
    url: "", actor: "dev", inputs: {}, title: "", created: "2026-11-25T10:00:00Z", started: "", stopped: "", card: "",
  };
}

function env(e: string, over: Partial<DeployEnv> = {}): DeployEnv {
  return {
    project: "alert", name: "Alert app", repo: "o/alert", env: e, workflow: `deploy-${e}.yml`,
    branch: "master", manual: false, read_at: "", ok_at: "2026-11-25T10:00:00Z", error: "", stale: false, run: null,
    waiting: [], confirm: "", skip_tests: false, launch: false, launched: null, deployed: null, head: H, pending: null,
    ahead: null, ahead_of: "", ...over,
  };
}

const LIST: DeployList = {
  envs: [
    env("test", { run: run("test", "success", H), deployed: run("test", "success", H), pending: 0, ahead: 3,
                  ahead_of: "prod" }),
    env("prod", { manual: true, launch: true, run: run("prod", "failure", "f".repeat(40)),
                  deployed: run("prod", "success", P), pending: 3 }),
    { ...env("prod"), project: "arm", name: "Arm app", stale: true, ok_at: "",
      error: "ответ Forgejo больше 8 МБ" },
  ],
  platform: null, now: "2026-11-25T10:00:00Z", stale_after: 120,
};

let fetch: ReturnType<typeof vi.fn>;

beforeEach(() => {
  setLocale("ru");
  // jsdom has no native modal implementation
  HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  HTMLDialogElement.prototype.close = function () { this.open = false; };
  fetch = vi.fn(async () => json(LIST));
  vi.stubGlobal("fetch", fetch);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const panel = async (name: string) =>
  within((await screen.findByRole("heading", { name })).closest("section") as HTMLElement);

describe("Deploys", () => {
  it("is called «Деплой»", async () => {
    render(Deploys);
    expect((await screen.findByRole("heading", { level: 1 })).textContent).toBe("Деплой");
  });

  it("shows each environment's status, what it runs and how far it is ahead of prod", async () => {
    render(Deploys);
    const alert = await panel("Alert app");
    const [test, prod] = alert.getAllByRole("button", { expanded: false }).filter((b) => b.classList.contains("ehead"));
    expect(within(test).getByText("успешно")).toBeTruthy();
    expect(within(test).getByText("+3 к prod")).toBeTruthy();
    expect(within(prod).getByText("упал")).toBeTruthy();
    expect(within(prod).getByText(/работает 11efc6c/)).toBeTruthy();  // the failed run is not what runs there
    expect(within(prod).getByText("ждут выката: 3")).toBeTruthy();
  });

  it("says in words why a project has no fresh data", async () => {
    render(Deploys);
    const arm = await panel("Arm app");
    const line = arm.getByRole("status");
    expect(line.textContent).toContain("Из Forgejo ещё ничего не прочитано");
    expect(line.textContent).toContain("ответ Forgejo больше 8 МБ");
    expect(arm.queryByText("устарел")).toBeNull();
  });

  it("opens the launch with the commit that goes out and no word to type", async () => {
    render(Deploys);
    const alert = await panel("Alert app");
    await fireEvent.click(alert.getByRole("button", { name: "Выкатить" }));
    const sheet = await screen.findByRole("dialog");
    expect(within(sheet).getByText("Уйдёт голова ветки master — 26711f9.")).toBeTruthy();
    expect(within(sheet).getByText("Новых коммитов против того, что сейчас на prod (11efc6c): 3.")).toBeTruthy();
    expect(within(sheet).queryByRole("textbox")).toBeNull();
    const go = within(sheet).getByRole("button", { name: "Выкатить" }) as HTMLButtonElement;
    expect(go.disabled).toBe(false);
  });
});
