// Trusted prepared bundle only: no caller-selected URL, config, test or browser flags.
import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: ".",
  testMatch: "isolated-smoke.spec.ts",
  outputDir: "../../../../tmp/artifacts",
  workers: 1,
  retries: 0,
  timeout: 45_000,
  reporter: "list",
  use: {
    ...devices["Desktop Chrome"],
    baseURL: "http://localhost:8765",
    locale: "ru-RU",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    launchOptions: { chromiumSandbox: true },
  },
  webServer: {
    cwd: "/runtime/app/common/cabinet/web",
    command: "node e2e/serve.mjs 8765 /runtime/app/tmp/cabinet-dev/e2e-isolated",
    url: "http://localhost:8765/login",
    reuseExistingServer: false,
    timeout: 45_000,
  },
  metadata: { state: "/runtime/app/tmp/cabinet-dev/e2e-isolated" },
});
