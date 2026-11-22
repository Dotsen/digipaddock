// The documentation's screenshots of the cabinet (docs/images/cabinet/): a fresh seeded dev cabinet
// (e2e/serve.mjs, the same seed as the browser tests), the English interface, a desktop and a phone.
// Usage: `npm run screenshots` (builds the app first); Python as for the e2e (KD_PYTHON or tmp/venv).
// Every name, server and address on them is the seed's: synthetic, of no deployment.
import { chromium, devices } from "@playwright/test";
import { spawn } from "node:child_process";
import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const root = path.resolve(web, "../../..");
const out = path.join(root, "docs", "images", "cabinet");
const port = 8790;
const base = `http://localhost:${port}`;
const state = path.join(root, "tmp", "cabinet-dev", "screenshots");

// [file, path or { card: its seeded title, tab, sign }]; sign — the signing dialog opened, nothing signed
const DESKTOP = [
  ["dashboard", "/"],
  ["inbox", "/inbox"],
  ["board", "/board"],
  ["card-thread", { card: "Card page in the cabinet" }],
  ["card-plan", { card: "web-1: refresh package indexes and restart nginx" }],
  ["card-plan-sign", { card: "web-1: refresh package indexes and restart nginx", sign: true }],
  ["card-brief", { card: "Daily error summary", tab: "brief" }],
  ["agents", "/agents"],
  ["servers", "/servers"],
  ["incidents", "/incidents"],
];
const PHONE = [
  ["phone-dashboard", "/"],
  ["phone-card", { card: "Card page in the cabinet" }],
];

async function waitFor(url, seconds = 60) {
  for (let i = 0; i < seconds * 4; i++) {
    try {
      if ((await fetch(url)).ok) return;
    } catch {
      // not listening yet
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`${url} did not answer`);
}

async function signIn(context) {
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  await cdp.send("WebAuthn.enable");
  await cdp.send("WebAuthn.addVirtualAuthenticator", {
    options: { protocol: "ctap2", transport: "internal", hasResidentKey: true, hasUserVerification: true, isUserVerified: true },
  });
  const codes = JSON.parse(readFileSync(path.join(state, "codes.json"), "utf-8"));
  await page.goto(`${base}/login`);
  await page.getByLabel("User").fill("owner");
  await page.getByLabel("One-time code").fill(codes.owner);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page.waitForURL(/\/setup$/);
  await page.getByRole("button", { name: "Create a passkey" }).click();
  await page.getByText("Passkey added").waitFor();
  await page.getByRole("button", { name: "Done" }).click();
  await page.waitForURL(/\/$/);
  return page;
}

async function shoot(page, shots) {
  const cards = await (await page.request.get(`${base}/api/cards`)).json();
  for (const [name, where] of shots) {
    let url = where;
    if (typeof where !== "string") {
      const card = cards.find((c) => c.title === where.card);
      if (!card) throw new Error(`no card «${where.card}»`);
      url = `/cards/${card.id}${where.tab ? `?tab=${where.tab}` : ""}`;
    }
    await page.goto(base + url);
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(700); // fonts and the live refresh settle
    if (where.sign) {
      await page.getByRole("button", { name: "Sign", exact: true }).click();
      await page.getByRole("dialog").waitFor();
      await page.waitForTimeout(300); // the dialog's entrance
    }
    await page.mouse.move(0, 0);
    await page.screenshot({ path: path.join(out, `${name}.png`) });
    console.log(`docs/images/cabinet/${name}.png`);
  }
}

mkdirSync(out, { recursive: true });
const server = spawn(process.execPath, [path.join(web, "e2e", "serve.mjs"), String(port), state, "en"], { stdio: ["ignore", "ignore", "inherit"] });
try {
  await waitFor(`${base}/login`);
  const browser = await chromium.launch();
  try {
    const desktop = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: "en-US", colorScheme: "dark" });
    const page = await signIn(desktop);
    await shoot(page, DESKTOP);
    const signedIn = await desktop.storageState();
    const phone = await browser.newContext({
      ...devices["iPhone 13"], deviceScaleFactor: 2, locale: "en-US", colorScheme: "dark", storageState: signedIn,
    });
    await shoot(await phone.newPage(), PHONE);
  } finally {
    await browser.close();
  }
} finally {
  server.kill();
}
