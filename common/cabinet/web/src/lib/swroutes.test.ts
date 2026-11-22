import { describe, expect, it } from "vitest";
import { isAppNavigation, safeAppPath } from "./swroutes";

describe("the service worker's navigation", () => {
  it("serves the app shell for the app's own screens only", () => {
    for (const path of ["/", "/inbox", "/cards/KD-5", "/decisions/abc", "/settings/users", "/login", "/servers",
      "/servers/app1", "/incidents/P-1"]) {
      expect(isAppNavigation(path), path).toBe(true);
    }
    // global review 15.10.2026: the monitor (basic auth), the legacy links nginx redirects into the
    // cabinet, the API, the live channel and the well-known files are the server's
    for (const path of [
      "/api/files/abc",
      "/ws",
      "/monitor",
      "/monitor/",
      "/monitor/P-1",
      "/monitor/s/stage",
      "/board/KD-5",
      "/admin/approvals/KD-5",
      "/boss/promotions/0123",
      "/.well-known/assetlinks.json",
      "/diagram.html", // the frame is never the app shell
    ]) {
      expect(isAppNavigation(path), path).toBe(false);
    }
  });

  it("opens a notification's link only as a path of this origin", () => {
    expect(safeAppPath("/cards/KD-5")).toBe("/cards/KD-5");
    for (const bad of ["//evil.example/x", "https://evil.example/x", "/\\evil.example/x", "/\n/evil.example/x", "", 5, undefined, "cards/KD-5"]) {
      expect(safeAppPath(bad), String(bad)).toBe("/inbox");
    }
  });
});
