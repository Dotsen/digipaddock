// real browser passkey signatures through the dev adapter of the root protocol.
import { expect } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { readFileSync, statSync, existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { api, getJson, ownerTest as test } from "./helpers";

test("an owner signs a provider key save and deletion; only metadata enters the feed", async ({ page, owner }, info) => {
  void owner;
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../..");
  const state = String(info.project.metadata.state);
  const python = process.env.KD_PYTHON ?? path.join(root, "tmp", "venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  // Root's explicit enrollment is a separate fixture action, never an HTTP route or runtime auto-enrollment.
  execFileSync(python, ["-c", [
    "import sys; sys.path.insert(0, sys.argv[1])",
    "from kd.cabinet import store, users",
    "from kd.model_keys import b64, enroll",
    "db=store.Store.open(sys.argv[2]); people=users.Users(db)",
    "class Cabinet:",
    " def call(self, op):",
    "  if op['op']=='user.list': return {'users':[{**u, 'roles':people.assignments(u['id'])} for u in people.list()]}",
    "  return {'keys':[{'credential_id':b64(k['credential_id']), 'public_key':b64(k['public_key'])} for k in people.signing_keys(op['user'])]}",
    "enroll('owner', b64(people.signing_keys('owner')[0]['credential_id'])[:12], Cabinet(), sys.argv[3])",
  ].join("\n"), path.join(root, "common"), path.join(state, "cabinet.db"), path.join(state, "model-keys", "signers")]);

  const value = "fixture-browser-provider-9218";
  const file = path.join(state, "model-keys", "llm", "openrouter.env");
  await page.goto("/settings/models?tab=keys");
  const form = page.locator("form").filter({ has: page.getByLabel("Ключ · OpenRouter", { exact: true }) });
  await form.getByLabel("Ключ · OpenRouter", { exact: true }).fill(value);
  await form.getByRole("button", { name: "Сохранить ключ с подписью" }).click();
  await expect(page.getByText("Ключ обновлён", { exact: true })).toBeVisible();
  await expect(form.getByLabel("Ключ · OpenRouter", { exact: true })).toHaveValue("");
  expect(readFileSync(file, "utf-8")).toBe(`ANTHROPIC_AUTH_TOKEN=${value}\n`);
  if (process.platform !== "win32") expect(statSync(file).mode & 0o777).toBe(0o600);
  const events = (await getJson(page, "/api/feed?kinds=llm_key.changed")).events;
  expect(events[0].payload).toEqual({ provider: "openrouter", action: "put" });
  expect(JSON.stringify(events)).not.toContain(value);
  expect((await api(page, "POST", "/api/models/providers/openrouter/key", { action: "delete" })).status).toBe(403);
  page.once("dialog", (dialog) => dialog.accept());
  await form.getByRole("button", { name: "Удалить ключ", exact: true }).click();
  await expect(page.getByText("Ключ удалён", { exact: true })).toBeVisible();
  expect(existsSync(file)).toBe(false);
  expect(readFileSync(path.join(state, "cabinet.db")).includes(Buffer.from(value))).toBe(false);

  // distinct credential destinations, replacement-only subscription and a real signed UI ceremony.
  const subscription = page.locator("form").filter({ has: page.getByLabel("Ключ · Подписка Claude", { exact: true }) });
  const subscriptionFile = path.join(state, "model-keys", "claude.env");
  await subscription.getByLabel("Ключ · Подписка Claude", { exact: true }).fill("fixture-browser-subscription");
  await subscription.getByRole("button", { name: "Сохранить ключ с подписью" }).click();
  await expect.poll(() => existsSync(subscriptionFile)).toBe(true);
  expect(readFileSync(subscriptionFile, "utf-8")).toBe("CLAUDE_CODE_OAUTH_TOKEN=fixture-browser-subscription\n");
  await expect(subscription.getByRole("button", { name: "Удалить ключ", exact: true })).toHaveCount(0);
  await expect(subscription.getByText(/Возраст: 0 дн/)).toBeVisible();
  await subscription.getByRole("button", { name: "Проверить", exact: true }).click();
  await expect(subscription.getByText(/Подписка работает/)).toBeVisible();
  for (const [title, kind, variable] of [["Anthropic API", "anthropic", "ANTHROPIC_API_KEY"], ["OpenAI API", "openai", "ANTHROPIC_AUTH_TOKEN"]]) {
    const form = page.locator("form").filter({ has: page.getByLabel(`Ключ · ${title}`, { exact: true }) });
    await form.getByLabel(`Ключ · ${title}`, { exact: true }).fill(`fixture-browser-${kind}`);
    await form.getByRole("button", { name: "Сохранить ключ с подписью" }).click();
    const file = path.join(state, "model-keys", "llm", `${kind}.env`);
    await expect.poll(() => existsSync(file)).toBe(true);
    expect(readFileSync(file, "utf-8")).toBe(`${variable}=fixture-browser-${kind}\n`);
  }
  const database = readFileSync(path.join(state, "cabinet.db"));
  for (const secret of ["fixture-browser-subscription", "fixture-browser-anthropic", "fixture-browser-openai"])
    expect(database.includes(Buffer.from(secret))).toBe(false);
});
