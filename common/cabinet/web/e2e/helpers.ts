// Shared steps of the e2e specs: sign-in codes of the seed, Chromium's virtual authenticator, the owner's
// session and passkey saved by login.spec for the other specs, API calls as the page's person.
import { test as base, expect, type Page, type TestInfo } from "@playwright/test";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

type Credential = Record<string, unknown>;

export interface Authenticator {
  send: (method: string, params?: Record<string, unknown>) => Promise<any>;
  id: string;
}

const stateOf = (info: TestInfo = base.info()) => String(info.project.metadata.state);
const authFile = (name: string, info?: TestInfo) => path.join(stateOf(info), "auth", name);

/** The one-time sign-in codes the seeded server printed (and wrote to codes.json). */
export function codes(): Record<string, string> {
  return JSON.parse(readFileSync(path.join(stateOf(), "codes.json"), "utf-8"));
}

// every page gets a higher signature counter than the last: the server refuses a counter going back
function nextSignCount(): number {
  const file = authFile("sign-count");
  const next = (existsSync(file) ? Number(readFileSync(file, "utf-8")) : 0) + 1000;
  writeFileSync(file, String(next));
  return next;
}

/** A virtual platform authenticator (resident keys, user verification) on this page; with `credential`,
 * that passkey is on it. */
export async function virtualAuthenticator(page: Page, credential?: Credential): Promise<Authenticator> {
  const cdp = await page.context().newCDPSession(page);
  const send = (method: string, params?: Record<string, unknown>) => cdp.send(method as any, params as any);
  await send("WebAuthn.enable");
  const { authenticatorId } = await send("WebAuthn.addVirtualAuthenticator", {
    options: { protocol: "ctap2", transport: "internal", hasResidentKey: true, hasUserVerification: true, isUserVerified: true },
  });
  if (credential) await send("WebAuthn.addCredential", { authenticatorId, credential: { ...credential, signCount: nextSignCount() } });
  return { send, id: authenticatorId };
}

export async function loginByCode(page: Page, user: string, code: string) {
  await page.goto("/login");
  await page.getByLabel("Пользователь").fill(user);
  await page.getByLabel("Одноразовый код").fill(code);
  await page.getByRole("button", { name: "Войти", exact: true }).click();
}

/** Sign out from the side menu (desktop) or the settings page (a phone has no side menu). */
export async function signOut(page: Page) {
  const side = page.locator("nav.side");
  if (await side.isVisible()) {
    await side.getByRole("button", { name: "Выйти" }).click();
  } else {
    await page.goto("/settings");
    await page.locator("main").getByRole("button", { name: "Выйти", exact: true }).click();
  }
  await page.waitForURL(/\/login/);
}

/** Keep the owner's session and passkey for the specs that run after login.spec. */
export async function saveOwner(page: Page, auth: Authenticator) {
  mkdirSync(path.dirname(authFile("owner.json")), { recursive: true });
  await page.context().storageState({ path: authFile("owner.json") });
  const { credentials } = await auth.send("WebAuthn.getCredentials", { authenticatorId: auth.id });
  expect(credentials).toHaveLength(1);
  writeFileSync(authFile("owner-credential.json"), JSON.stringify(credentials[0]));
}

/** Specs that run as the owner: the saved session, and `owner` puts the saved passkey on the page. */
export const ownerTest = base.extend<{ owner: Authenticator }>({
  storageState: async ({}, use, info) => use(authFile("owner.json", info)),
  owner: async ({ page }, use) => {
    const credential = JSON.parse(readFileSync(authFile("owner-credential.json"), "utf-8"));
    await use(await virtualAuthenticator(page, credential));
  },
});

/** A call to the cabinet's API as the page's person (cookie + CSRF token). */
export async function api(page: Page, method: string, url: string, body?: unknown) {
  return page.evaluate(
    async ([method, url, body]) => {
      const me = await (await fetch("/api/me")).json();
      const response = await fetch(url as string, {
        method: method as string,
        headers: { "Content-Type": "application/json", "X-CSRF-Token": me.csrf },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const text = await response.text();
      return { status: response.status, body: text ? JSON.parse(text) : null };
    },
    [method, url, body] as const,
  );
}

export const getJson = (page: Page, url: string) => api(page, "GET", url).then((r) => r.body);

/** One board operation as the executor of an agent's kd-task / kd-work (the dev cabinet only). */
export async function executor(page: Page, op: Record<string, unknown>) {
  const result = await api(page, "POST", "/api/dev/executor", op);
  expect(result.status, JSON.stringify(result.body)).toBe(200);
  return result.body;
}

export async function cardByTitle(page: Page, title: string) {
  const cards: { id: string; title: string }[] = await getJson(page, "/api/cards");
  const card = cards.find((c) => c.title === title);
  if (!card) throw new Error(`no card «${title}»`);
  return card;
}
