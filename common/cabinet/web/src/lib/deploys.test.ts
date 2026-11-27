// the «Deploys» page's journal address, filters and status colours.
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  checkMark, HeadChanged, commitGroups, deltas, envsOf, inputsText, JOURNAL_STEP, journalUrl, journalView, launchDeploy, live, projectRows,
  projectsOf, settled, shortSha, statusClass, statusMark,
} from "./deploys";
import type { DeployCommit, DeployEnv, DeployRun } from "./types";

const RUN: DeployRun = {
  project: "api", env: "prod", run: 1, number: 1, workflow: "deploy-prod.yml", status: "success", event: "push",
  sha: "a".repeat(40), ref: "main", url: "", actor: "dev", inputs: {}, title: "", created: "2026-11-25T10:00:00Z",
  started: "", stopped: "", card: "",
};

function env(project: string, name: string, e: string): DeployEnv {
  return {
    project, name, repo: `o/${project}`, env: e, workflow: `deploy-${e}.yml`, branch: "main", manual: false,
    read_at: "", ok_at: "", error: "", stale: false, run: null, waiting: [], confirm: "", skip_tests: false,
    launch: false, launched: null, deployed: null, head: "", pending: null, pending_commits: [], ahead: null,
    ahead_commits: [], ahead_of: "", in_flight: false,
  };
}

const commit = (sha: string, title: string): DeployCommit => ({
  sha: sha.repeat(40), title, body: "", author: "dev", date: "2026-11-24T18:10:59Z", url: "",
});

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

afterEach(() => vi.unstubAllGlobals());

const SHOWN = "a".repeat(40);
const MOVED = "b".repeat(40);

describe("the owner's prod launch", () => {
  it("asks a challenge with skip_tests and no typed word, signs it and sends the signature", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json({ challenge_id: "c1", options: { challenge: "v1", userVerification: "discouraged" },
                                    expires: "", launch: { sha: SHOWN } }))
      .mockResolvedValueOnce(json({ state: "dispatched", sha: "c".repeat(40), project: "api", env: "prod",
                                    skip_tests: true }));
    vi.stubGlobal("fetch", fetch);
    const sign = vi.fn(async (options: Record<string, unknown>) => ({ id: "cred", over: options.challenge }));
    const done = await launchDeploy({ project: "api", env: "prod" }, true, SHOWN, sign);
    expect(done.state).toBe("dispatched");
    expect(sign).toHaveBeenCalledWith({ challenge: "v1", userVerification: "discouraged" });
    expect(fetch.mock.calls.map(([url, init]) => [url, JSON.parse(init.body)])).toEqual([
      ["/api/deploys/launch/challenge", { project: "api", env: "prod", skip_tests: true, expected_sha: SHOWN }],
      ["/api/deploys/launch", { challenge_id: "c1", credential: { id: "cred", over: "v1" } }],
    ]);
  });

  it("names the commit the sheet showed and never signs when the branch moved since", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(json({ error: "conflict", message: "moved", reason: "branch_moved",
                                                       head: MOVED, branch: "main", expected: SHOWN }, 409));
    vi.stubGlobal("fetch", fetch);
    const sign = vi.fn();
    const error = await launchDeploy({ project: "api", env: "prod" }, false, SHOWN, sign).catch((e) => e);
    expect(error).toBeInstanceOf(HeadChanged);
    expect(error.head).toBe(MOVED);
    expect(error.unseen).toBe(false);
    expect(sign).not.toHaveBeenCalled();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("asks the head first when the sheet knew none: the answer is the head, no signature", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(json({ error: "conflict", message: "look", reason: "head_unseen",
                                                       head: MOVED, branch: "main" }, 409));
    vi.stubGlobal("fetch", fetch);
    const sign = vi.fn();
    const error = await launchDeploy({ project: "api", env: "prod" }, false, "", sign).catch((e) => e);
    expect(error).toBeInstanceOf(HeadChanged);
    expect([error.head, error.unseen]).toEqual([MOVED, true]);
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ project: "api", env: "prod", skip_tests: false });
    expect(sign).not.toHaveBeenCalled();
  });

  it("does not sign a challenge that binds another commit than the shown one", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json({
      challenge_id: "c1", options: { challenge: "v1" }, expires: "",
      launch: { project: "api", env: "prod", branch: "main", sha: MOVED, skip_tests: false } })));
    const sign = vi.fn();
    const error = await launchDeploy({ project: "api", env: "prod" }, false, SHOWN, sign).catch((e) => e);
    expect(error).toBeInstanceOf(HeadChanged);
    expect(error.head).toBe(MOVED);
    expect(sign).not.toHaveBeenCalled();
  });

  it("asks no signature when the cabinet refuses the challenge", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json({ error: "refused", message: "owner only" }, 403)));
    const sign = vi.fn();
    await expect(launchDeploy({ project: "api", env: "prod" }, false, SHOWN, sign)).rejects.toThrow();
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

  it("marks a status with a sign, not with colour alone", () => {
    expect(statusMark("success")).toBe("✓");
    expect(statusMark("failure")).toBe("✗");
    expect(statusMark("running")).toBe("…");
    expect(statusMark("waiting")).toBe("…");
    expect(statusMark("cancelled")).toBe("–");
    expect(statusMark(undefined)).toBe("–");
  });

  it("puts each project in one panel with its environments in the page's order and its read's state", () => {
    const stale = { ...env("api", "Api", "stage"), stale: true, error: "Forgejo 500", ok_at: "2026-11-25T10:00:00Z" };
    const rows = projectRows([env("alert", "Alert", "test"), env("alert", "Alert", "prod"), stale]);
    expect(rows.map((r) => [r.id, r.envs.map((e) => e.env), r.stale, r.error, r.okAt, r.launch])).toEqual([
      ["alert", ["test", "prod"], false, "", "", false],
      ["api", ["stage"], true, "Forgejo 500", "2026-11-25T10:00:00Z", false],
    ]);
    expect(projectRows([{ ...env("api", "Api", "prod"), launch: true }])[0].launch).toBe(true);
  });

  it("says how far an environment is ahead of prod and what waits to be deployed", () => {
    const base = env("alert", "Alert", "test");
    expect(deltas({ ...base, ahead: 3, ahead_of: "prod", pending: 0 })).toEqual([
      { key: "deploys.ahead", params: { n: 3, env: "prod" }, tone: "acc" },
    ]);
    expect(deltas({ ...base, ahead: 0, ahead_of: "prod" })).toEqual([
      { key: "deploys.even", params: { env: "prod" }, tone: "muted" },
    ]);
    const prod = { ...env("alert", "Alert", "prod"), manual: true };
    expect(deltas({ ...prod, pending: 3 })).toEqual([{ key: "deploys.pending", params: { n: 3 }, tone: "warn" }]);
    expect(deltas({ ...prod, pending: 0 })).toEqual([{ key: "deploys.up_to_date", params: {}, tone: "ok" }]);
    // a test environment behind its own branch (its deploy failed) says so beside its lead
    expect(deltas({ ...base, ahead: 1, ahead_of: "prod", pending: 2 }).map((d) => d.key)).toEqual([
      "deploys.ahead", "deploys.pending"]);
    expect(deltas(base)).toEqual([]);  // nothing measured: nothing said
  });

  it("lists the commits behind each count, and how many more there are than the reader listed", () => {
    const listed = [commit("a", "feat: a"), commit("b", "fix: b")];
    const prod = { ...env("alert", "Alert", "prod"), pending: 25, pending_commits: listed };
    expect(commitGroups(prod)).toEqual([
      { key: "deploys.commits.pending", params: { env: "prod", n: 25 }, commits: listed, more: 23 },
    ]);
    const test = { ...env("alert", "Alert", "test"), ahead: 2, ahead_commits: listed, ahead_of: "prod", pending: 0 };
    expect(commitGroups(test)).toEqual([
      { key: "deploys.commits.ahead", params: { env: "test", ref: "prod", n: 2 }, commits: listed, more: 0 },
    ]);
    // a count without its list (a reader of before the lists) and nothing to list say nothing
    expect(commitGroups({ ...prod, pending_commits: [] })).toEqual([]);
    expect(commitGroups({ ...prod, pending: 0 })).toEqual([]);
    expect(commitGroups({ ...env("alert", "Alert", "prod"), pending: 3 } as DeployEnv)).toEqual([]);
  });

  it("says in the row how many of the listed commits failed their checks", () => {
    const failed = { ...commit("a", "feat: a"), status: "failure" as const };
    const prod = { ...env("alert", "Alert", "prod"), manual: true, pending: 3,
                   pending_commits: [failed, commit("b", "fix: b"), { ...commit("c", "c"), status: "pending" as const }] };
    expect(deltas(prod)).toEqual([
      { key: "deploys.pending", params: { n: 3 }, tone: "warn" },
      { key: "deploys.checks_failed", params: { n: 1 }, tone: "crit" },
    ]);
    // the same commit in both lists counts once; a list of before the checks says nothing
    const test = { ...env("alert", "Alert", "test"), ahead: 1, ahead_of: "prod", ahead_commits: [failed],
                   pending: 1, pending_commits: [failed] };
    expect(deltas(test).find((d) => d.key === "deploys.checks_failed")?.params).toEqual({ n: 1 });
    expect(deltas({ ...prod, pending_commits: [commit("a", "a")] }).map((d) => d.key)).toEqual(["deploys.pending"]);
  });

  it("does not count a commit whose run a newer push cancelled as failed", () => {
    const stopped = { ...commit("a", "docs: a"), status: "cancelled" as const };
    const prod = { ...env("alert", "Alert", "prod"), manual: true, pending: 1, pending_commits: [stopped] };
    expect(deltas(prod).map((d) => d.key)).toEqual(["deploys.pending"]);
  });

  it("calls an environment settled when everything is deployed there and its last run passed", () => {
    const prod = { ...env("alert", "Alert", "prod"), manual: true, pending: 0, run: RUN };
    expect(settled(prod)).toBe(true);
    expect(settled({ ...prod, pending: 2 })).toBe(false);  // commits wait
    expect(settled({ ...prod, pending: null })).toBe(false);  // unknown
    expect(settled({ ...prod, run: { ...RUN, status: "failure" } })).toBe(false);
    expect(settled({ ...prod, run: null })).toBe(false);
    expect(settled({ ...prod, in_flight: true })).toBe(false);  // a deploy under way
    expect(settled({ ...prod, stale: true })).toBe(false);  // no fresh data: nothing to vouch for
    // an environment measured against prod says «как prod», not «всё выкачено»: it is not painted
    expect(settled({ ...env("alert", "Alert", "test"), pending: 0, run: RUN, ahead: 0, ahead_of: "prod" })).toBe(false);
  });

  it("marks a commit's checks with a sign and a colour", () => {
    expect(checkMark("success")).toEqual({ mark: "✓", tone: "ok" });
    expect(checkMark("failure")).toEqual({ mark: "✗", tone: "crit" });
    expect(checkMark("pending")).toEqual({ mark: "…", tone: "warn" });
    expect(checkMark("warning")).toEqual({ mark: "!", tone: "warn" });
    expect(checkMark("cancelled")).toEqual({ mark: "⊘", tone: "muted" });
    expect(checkMark("")).toBeNull();
    expect(checkMark(undefined)).toBeNull();
  });

  it("names what runs on the environment when its last run is not it", () => {
    const run = (status: DeployRun["status"], sha: string) => ({ ...RUN, status, sha });
    const base = env("api", "Api", "prod");
    expect(live({ ...base, run: run("failure", "b".repeat(40)), deployed: run("success", "a".repeat(40)) }))
      .toEqual(run("success", "a".repeat(40)));
    expect(live({ ...base, run: run("success", "a".repeat(40)), deployed: run("success", "a".repeat(40)) }))
      .toBeNull();
    expect(live({ ...base, run: run("running", "b".repeat(40)), deployed: null })).toBeNull();
  });

  it("shows the journal by pages of twenty and asks the server when the loaded runs end", () => {
    expect(JOURNAL_STEP).toBe(20);
    expect(journalView(45, 20, true)).toEqual({ shown: 20, more: true, fetch: false });
    expect(journalView(45, 60, true)).toEqual({ shown: 45, more: true, fetch: true });
    expect(journalView(45, 60, false)).toEqual({ shown: 45, more: false, fetch: false });
    expect(journalView(10, 20, false)).toEqual({ shown: 10, more: false, fetch: false });
  });
});
