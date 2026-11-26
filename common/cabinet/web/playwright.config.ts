// End-to-end tests against the dev cabinet: `kd-cabinet serve --dev --seed`, one fresh server per
// device kind (desktop on 8765, a phone on 8766), each with its own state under tmp/cabinet-dev/. Passkeys
// come from Chromium's virtual authenticator (CDP), so the phone runs Chromium with iPhone 13's screen.
// `npm run e2e` builds the app first; the server serves dist/.
import { defineConfig, devices } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "../../..");
const run = (process.env.KD_E2E_RUN ?? "").replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 40);
const state = (tag: string) => path.join(root, "tmp", "cabinet-dev", `e2e-${run ? `${run}-` : ""}${tag}`);
const DESKTOP = "http://localhost:8765";
const PHONE = "http://localhost:8766";
const desktop = { ...devices["Desktop Chrome"], baseURL: DESKTOP, locale: "ru-RU" };
const phone = { ...devices["iPhone 13"], browserName: "chromium" as const, baseURL: PHONE, locale: "ru-RU" };

export default defineConfig({
  testDir: "e2e",
  outputDir: path.join(root, "tmp", "cabinet-dev", "playwright"),
  fullyParallel: false,
  workers: 1, // one seeded server per device kind: the specs share its state
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"]],
  use: { trace: "retain-on-failure", screenshot: "only-on-failure" },
  webServer: [
    { command: `node e2e/serve.mjs 8765 "${state("desktop")}"`, url: `${DESKTOP}/login`, reuseExistingServer: false, timeout: 60_000 },
    { command: `node e2e/serve.mjs 8766 "${state("phone")}"`, url: `${PHONE}/login`, reuseExistingServer: false, timeout: 60_000 },
  ],
  projects: [
    { name: "setup-desktop", testMatch: /login\.spec\.ts/, use: desktop, metadata: { state: state("desktop") } },
    {
      name: "desktop-chromium",
      testMatch: /(board|brief|card-look|checklists|council|decisions|deploys|diagram|live|model-keys|monitoring|nav|overview|partner|pipeline|questions|result|review|sessions|widths)\.spec\.ts/,
      dependencies: ["setup-desktop"],
      use: desktop,
      metadata: { state: state("desktop") },
    },
    { name: "setup-mobile", testMatch: /login\.spec\.ts/, use: phone, metadata: { state: state("phone") } },
    {
      name: "mobile",
      testMatch: /(decisions|mobile|model-keys|monitoring|nav|sessions)\.spec\.ts/,
      dependencies: ["setup-mobile"],
      use: phone,
      metadata: { state: state("phone") },
    },
  ],
});
