// the «Deploys» page's journal address, filters and status colours.
import { afterEach, describe, expect, it, vi } from "vitest";
import { envsOf, inputsText, journalUrl, launchDeploy, projectsOf, shortSha, statusClass } from "./deploys";
import type { DeployEnv } from "./types";

function env(project: string, name: string, e: string): DeployEnv {
  return {
    project, name, repo: `o/${project}`, env: e, workflow: `deploy-${e}.yml`, branch: "main", manual: false,
    read_at: "", ok_at: "", error: "", stale: false, run: null, waiting: [], confirm: "", skip_tests: false,
    launch: false, launched: null,
  };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

afterEach(() => vi.unstubAllGlobals());

describe("the owner's prod launch", () => {
  it("asks a challenge with the typed word and skip_tests, signs it and sends the signature", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json({ challenge_id: "c1", options: { challenge: "v1", userVerification: "discouraged" },
                                    expires: "", launch: {} }))
      .mockResolvedValueOnce(json({ state: "dispatched", sha: "c".repeat(40), project: "api", env: "prod",
                                    skip_tests: true }));
    vi.stubGlobal("fetch", fetch);
    const sign = vi.fn(async (options: Record<string, unknown>) => ({ id: "cred", over: options.challenge }));
    const done = await launchDeploy({ project: "api", env: "prod" }, "deploy", true, sign);
    expect(done.state).toBe("dispatched");
    expect(sign).toHaveBeenCalledWith({ challenge: "v1", userVerification: "discouraged" });
    expect(fetch.mock.calls.map(([url, init]) => [url, JSON.parse(init.body)])).toEqual([
      ["/api/deploys/launch/challenge", { project: "api", env: "prod", confirm: "deploy", skip_tests: true }],
      ["/api/deploys/launch", { challenge_id: "c1", credential: { id: "cred", over: "v1" } }],
    ]);
  });

  it("asks no signature when the cabinet refuses the challenge", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json({ error: "refused", message: "owner only" }, 403)));
    const sign = vi.fn();
    await expect(launchDeploy({ project: "api", env: "prod" }, "deploy", false, sign)).rejects.toThrow();
    expect(sign).not.toHaveBeenCalled();
  });
});

describe("the deploys page", () => {
  it("asks the journal for the filters set and the page before a moment", () => {
    expect(journalUrl("", "")).toBe("/api/deploys/runs");
    expect(journalUrl("api", "prod")).toBe("/api/deploys/runs?project=api&env=prod");
    expect(journalUrl("", "prod")).toBe("/api/deploys/runs");  // an env without its project means nothing
    expect(journalUrl("alert", "", "2026-10-14T09:00:00Z")).toBe(
      "/api/deploys/runs?project=alert&before=2026-10-14T09%3A00%3A00Z");
  });

  it("lists the projects once in the page's order and each one's environments", () => {
    const envs = [env("alert", "Alert", "test"), env("alert", "Alert", "prod"), env("api", "Api", "staging")];
    expect(projectsOf(envs)).toEqual([{ id: "alert", name: "Alert" }, { id: "api", name: "Api" }]);
    expect(envsOf(envs, "alert")).toEqual(["test", "prod"]);
    expect(envsOf(envs, "platform")).toEqual(["release"]);
  });

  it("colours a status and shortens a commit", () => {
    expect(statusClass("success")).toBe("ok");
    expect(statusClass("failure")).toBe("crit");
    expect(statusClass("running")).toBe("warn");
    expect(statusClass("cancelled")).toBe("off");
    expect(statusClass(undefined)).toBe("off");
    expect(shortSha("0123456789abcdef")).toBe("0123456");
    expect(inputsText({ skip_tests: "true", confirm: "deploy" })).toBe("skip_tests=true, confirm=deploy");
  });
});
