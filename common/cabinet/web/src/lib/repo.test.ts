import { describe, expect, it } from "vitest";
import { repoWaiting, validRepo } from "./repo";

describe("validRepo", () => {
  it("takes what the server takes (kd.cabinet.projects_api.REPO)", () => {
    for (const value of ["example/DigiPaddock", "a/b", "A_1/r.e-p", `${"o".repeat(64)}/${"r".repeat(100)}`]) {
      expect(validRepo(value)).toBe(true);
    }
  });

  it("refuses what the server refuses (common/tests/test_cabinet_project_repos.py)", () => {
    for (const value of [
      "Site", "/Site", "example/", "example/Site/x", "kon stantin/Site", "example/.claude", ".x/Site",
      "example/a..b", "-o/Site", `example/${"x".repeat(101)}`, `${"o".repeat(65)}/Site`, "example/Site;rm", "",
    ]) {
      expect(validRepo(value)).toBe(false);
    }
  });
});

describe("repoWaiting", () => {
  it("is an open request only", () => {
    expect(repoWaiting("pending")).toBe(true);
    expect(repoWaiting("unbinding")).toBe(true);
    for (const state of ["", "connected", "no_access", "no_key", "hooks", "error", undefined]) {
      expect(repoWaiting(state)).toBe(false);
    }
  });
});
