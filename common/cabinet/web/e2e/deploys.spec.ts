import { expect } from "@playwright/test";
import { ownerTest as test } from "./helpers";

// the page's API answer is the test's own: a service worker would fetch it past page.route
test.use({ serviceWorkers: "block" });

// «Деплой» with the reader's data as stage has it: three projects, a failed prod run, a read that failed.
const H = "26711f9" + "0".repeat(33);
const P = "11efc6c" + "0".repeat(33);
const run = (project: string, env: string, status: string, sha: string, created = "2026-11-25T08:00:00Z") => ({
  project, env, run: 1, number: 7, workflow: `deploy-${env}.yml`, status, event: "push", sha, ref: "master",
  url: "https://forge.example.test/o/alert/actions/runs/1", actor: "dev", inputs: {}, title: "", created, started: "",
  stopped: "", card: "",
});
const env = (project: string, name: string, e: string, over: Record<string, unknown> = {}) => ({
  project, name, repo: `o/${project}`, env: e, workflow: `deploy-${e}.yml`, branch: "master", manual: false,
  read_at: "", ok_at: "2026-11-25T10:00:00Z", error: "", stale: false, run: null, waiting: [], confirm: "",
  skip_tests: false, launch: false, launched: null, deployed: null, head: H, pending: null, pending_commits: [],
  ahead: null, ahead_commits: [], ahead_of: "", ...over,
});
const commit = (sha: string, title: string, body: string, author: string, date: string, status = "success",
                checks: { context: string; state: string; url: string }[] = []) => ({
  sha, title, body, author, date, url: `https://forge.example.test/o/alert/commit/${sha}`, status, checks,
});
const WAITS = [
  commit(H, "fix(status): тихий прогон пишет статус, громкий пропуск уведомляет",
         "Тихий режим больше не прячет запись статуса.\n\n- silent: пишет статус, без уведомления\n"
         + "- громкий пропуск: уведомляет владельца", "dev", "2026-11-24T18:10:59Z"),
  commit("8".repeat(40), "fix(api): retry the token once on 401", "", "dev", "2026-11-24T15:02:00Z", "failure",
         [{ context: "Test & Deploy / Run tests (push)", state: "failure",
            url: "https://forge.example.test/o/alert/actions/runs/31/jobs/0" }]),
  commit("7".repeat(40), "docs: deploy notes", "", "Ada", "2026-11-23T09:40:00Z"),
];
const LIST = {
  envs: [
    env("alert", "Alert app", "test", { run: run("alert", "test", "success", H), pending: 0, ahead: 3,
                                                 ahead_commits: WAITS, ahead_of: "prod",
                                                 waiting: [{ card: "KD-9450", sha: H }] }),
    env("alert", "Alert app", "stage", { branch: "dev", run: run("alert", "stage", "running", "9".repeat(40)),
                                                  ahead: 2, ahead_of: "prod" }),
    env("alert", "Alert app", "prod", { manual: true, launch: true, pending: 3, pending_commits: WAITS,
                                                 run: run("alert", "prod", "failure", "f".repeat(40)),
                                                 deployed: run("alert", "prod", "success", P, "2026-11-22T08:00:00Z") }),
    env("api", "Api app", "staging", { branch: "main", run: run("api", "staging", "success", H), pending: 0,
                                               ahead: 0, ahead_of: "prod" }),
    env("api", "Api app", "prod", { branch: "main", manual: true, launch: true, skip_tests: true, pending: 0,
                                            in_flight: true,
                                            run: run("api", "prod", "success", H) }),
    env("arm", "Arm app", "prod", { stale: true, ok_at: "", error: "ответ Forgejo больше 8 МБ" }),
  ],
  platform: { ...run("platform", "release", "success", "84903f4"), card: "KD-9434" },
  now: "2026-11-25T10:00:00Z", stale_after: 120,
};

test("«Деплой»: every environment's status and counts fit narrow and wide screens, the buttons stay in one column", async ({ page }) => {
  await page.route("**/api/deploys", (route) => route.fulfill({ json: LIST }));
  for (const width of [320, 390, 768, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/deploys");
    await expect(page.getByRole("heading", { level: 1, name: "Деплой" })).toBeVisible();
    await expect(page.getByText("+3 к prod")).toBeVisible();
    await expect(page.getByText("ждут выката: 3")).toBeVisible();
    await expect(page.getByText(/Из Forgejo ещё ничего не прочитано/)).toBeVisible();
    await expect(page.locator(".erow", { has: page.locator(".ename", { hasText: /^prod$/ }) }).first()
      .getByText("упали проверки: 1")).toBeVisible();
    await expect(page.getByRole("button", { name: "Выкатывается…" })).toBeDisabled();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `width ${width}`).toBe(true);
    const [alert, api] = await page.locator(".act .btn").all();  // api's waits for its deploy under way
    const a = (await alert.boundingBox())!;
    const b = (await api.boundingBox())!;
    expect(Math.abs(a.x - b.x), `buttons aligned at ${width}`).toBeLessThan(1);
    expect(Math.abs(a.width - b.width), `buttons of one width at ${width}`).toBeLessThan(1);
    await page.screenshot({ path: `../../../tmp/cabinet-dev/deploys-${width}.png`, fullPage: true });
    // prod's row opens on the commits that wait: a title a line, the description on a tap, the hash links to Forgejo
    await page.locator(".erow", { has: page.locator(".ename", { hasText: /^prod$/ }) }).first()
      .locator(".ehead").click();
    const waits = page.getByRole("list", { name: "Не выкачено на prod: 3" });
    await expect(waits.getByRole("listitem")).toHaveCount(3);
    await waits.getByText(/^fix\(status\)/).click();
    await expect(waits.getByText(/Тихий режим больше не прячет/)).toBeVisible();
    await expect(waits.getByRole("link", { name: /Run tests/ })).toHaveAttribute(
      "href", "https://forge.example.test/o/alert/actions/runs/31/jobs/0");
    await expect(waits.getByRole("link", { name: "26711f9" })).toHaveAttribute(
      "href", `https://forge.example.test/o/alert/commit/${H}`);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `open, ${width}`).toBe(true);
    await page.screenshot({ path: `../../../tmp/cabinet-dev/deploys-${width}-open.png`, fullPage: true });
  }
  await page.getByRole("button", { name: "Выкатить" }).first().click();
  const sheet = page.getByRole("dialog");
  await expect(sheet).toContainText("Уйдёт голова ветки master — 26711f9.");
  await expect(sheet).toContainText("Новых коммитов против того, что сейчас на prod (11efc6c): 3.");
  await expect(sheet.getByRole("textbox")).toHaveCount(0);
  await expect(sheet.getByRole("list", { name: "Не выкачено на prod: 3" }).getByRole("listitem")).toHaveCount(3);
  await expect(sheet).toContainText("У 1 из уходящих коммитов упали проверки.");
  await page.screenshot({ path: "../../../tmp/cabinet-dev/deploys-launch.png" });
});
