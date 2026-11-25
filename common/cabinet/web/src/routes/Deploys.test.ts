// «Деплой»: a panel per project with a row per environment — its status, what it runs, how far it is ahead of prod
// and what waits; a project whose read failed says so in words; the owner's launch asks for the passkey alone.
import { cleanup, fireEvent, render, screen, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import type { DeployCommit, DeployEnv, DeployList, DeployRun } from "../lib/types";
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
    pending_commits: [], ahead: null, ahead_commits: [], ahead_of: "", in_flight: false, ...over,
  };
}

const WAITS: DeployCommit[] = [
  { sha: H, title: "fix(status): тихий прогон пишет статус", body: "Громкий пропуск уведомляет.",
    author: "dev", date: "2026-11-24T18:10:59Z", url: `https://forge.example.test/o/alert/commit/${H}`,
    status: "failure", checks: [{ context: "CI / Run tests (push)", state: "failure",
                                  url: "https://forge.example.test/o/alert/actions/runs/9/jobs/0" }] },
  { sha: "c".repeat(40), title: "fix: одна строка", body: "", author: "Ada", date: "2026-11-24T17:00:00Z",
    url: "" },
];

const LIST: DeployList = {
  envs: [
    env("test", { run: run("test", "success", H), deployed: run("test", "success", H), pending: 0, ahead: 3,
                  ahead_of: "prod" }),
    env("prod", { manual: true, launch: true, run: run("prod", "failure", "f".repeat(40)),
                  deployed: run("prod", "success", P), pending: 3, pending_commits: WAITS }),
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

  it("opens a row on the commits that wait: title, author, when, a link and the description", async () => {
    render(Deploys);
    const alert = await panel("Alert app");
    const prod = alert.getAllByRole("button").find((b) => b.querySelector(".ename")?.textContent === "prod")!;
    await fireEvent.click(prod);
    const list = alert.getByRole("list", { name: "Не выкачено на prod: 3" });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0].textContent).toContain("fix(status): тихий прогон пишет статус");
    expect(items[0].textContent).toContain("dev");
    expect(items[0].textContent).toContain("Громкий пропуск уведомляет.");
    const link = within(items[0]).getByRole("link", { name: "26711f9" }) as HTMLAnchorElement;
    expect(link.href).toBe(`https://forge.example.test/o/alert/commit/${H}`);
    expect(within(items[1]).queryByRole("link")).toBeNull();  // no page in Forgejo: the hash alone
    expect(within(list.parentElement as HTMLElement).getByText("…и ещё 1 постарше")).toBeTruthy();
  });

  it("says which waiting commit failed its checks, in the row and under the commit with its log", async () => {
    render(Deploys);
    const alert = await panel("Alert app");
    const prod = alert.getAllByRole("button").find((b) => b.querySelector(".ename")?.textContent === "prod")!;
    expect(within(prod).getByText("упали проверки: 1")).toBeTruthy();
    await fireEvent.click(prod);
    const [first] = within(alert.getByRole("list", { name: "Не выкачено на prod: 3" })).getAllByRole("listitem");
    expect(within(first).getByTitle("проверки упали")).toBeTruthy();
    const log = within(first).getByRole("link", { name: /CI \/ Run tests \(push\)/ }) as HTMLAnchorElement;
    expect(log.href).toBe("https://forge.example.test/o/alert/actions/runs/9/jobs/0");
  });

  it("paints a row green when everything is deployed there, and its button grey", async () => {
    fetch.mockImplementation(async () => json({ ...LIST, envs: [
      env("prod", { manual: true, launch: true, run: run("prod", "success", H), deployed: run("prod", "success", H),
                    pending: 0 })] }));
    render(Deploys);
    const alert = await panel("Alert app");
    expect(alert.getByText("всё выкачено")).toBeTruthy();
    const button = alert.getByRole("button", { name: "Выкатить" }) as HTMLButtonElement;
    expect(button.closest(".erow")?.classList.contains("settled")).toBe(true);
    expect(button.classList.contains("primary")).toBe(false);
    expect(button.disabled).toBe(false);  // a redeploy of the same commit stays possible
  });

  it("keeps the button bright while commits wait", async () => {
    render(Deploys);
    const alert = await panel("Alert app");
    const button = alert.getByRole("button", { name: "Выкатить" });
    expect(button.classList.contains("primary")).toBe(true);
    expect(button.closest(".erow")?.classList.contains("settled")).toBe(false);
  });

  it("holds the button while a deploy is under way", async () => {
    fetch.mockImplementation(async () => json({ ...LIST, envs: LIST.envs.map((e) =>
      e.project === "alert" && e.env === "prod" ? { ...e, in_flight: true } : e) }));
    render(Deploys);
    const alert = await panel("Alert app");
    const button = alert.getByRole("button", { name: "Выкатывается…" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(alert.queryByRole("button", { name: "Выкатить" })).toBeNull();
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
    expect(within(sheet).getByText("fix(status): тихий прогон пишет статус")).toBeTruthy();
    expect(within(sheet).getByText("У 1 из уходящих коммитов упали проверки.")).toBeTruthy();
    expect(within(sheet).queryByRole("textbox")).toBeNull();
    const go = within(sheet).getByRole("button", { name: "Выкатить" }) as HTMLButtonElement;
    expect(go.disabled).toBe(false);
  });
});
