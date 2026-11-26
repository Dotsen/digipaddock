// The «Deploy» page: the journal's address, its filters, how a run's status looks, what an environment says about
// its commits; the owner's prod launch.
import { ApiError, apiPost } from "./api";
import type { Key } from "./i18n.svelte";
import type { CheckState, DeployChallenge, DeployCommit, DeployEnv, DeployRun } from "./types";
import { assertPasskey } from "./webauthn";

export const PLATFORM = "platform";
export const RELEASE_ENV = "release";

export interface Launched {
  state: string;
  sha: string;
  project: string;
  env: string;
  skip_tests: boolean;
}

/** The branch's head is not the commit the sheet showed (`unseen`: the sheet showed none). The page shows `head` and
 *  its commits again, and the owner looks before launching: nothing was signed. */
export class HeadChanged extends Error {
  constructor(
    public head: string,
    public unseen: boolean,
  ) {
    super(unseen ? "the sheet showed no head of the branch" : "the branch moved since the page was read");
    this.name = "HeadChanged";
  }
}

/** The launch: a challenge over the commit the sheet showed (`shown`, '' when it showed none — the server then only
 *  answers the head), the saved passkey's signature without a fingerprint or PIN (the server asks
 *  `userVerification: discouraged`), then kd-review dispatches it. A head other than the shown one is `HeadChanged`
 *  before any prompt, as a card revision is in decide.ts. No typed word: the passkey is the confirmation. */
export async function launchDeploy(
  row: Pick<DeployEnv, "project" | "env">,
  skipTests: boolean,
  shown: string,
  sign: (options: Record<string, unknown>) => Promise<unknown> = assertPasskey,
): Promise<Launched> {
  let challenge: DeployChallenge;
  try {
    challenge = await apiPost<DeployChallenge>("/api/deploys/launch/challenge", {
      project: row.project,
      env: row.env,
      skip_tests: skipTests,
      ...(shown ? { expected_sha: shown } : {}),
    });
  } catch (exc) {
    const reason = exc instanceof ApiError && exc.status === 409 ? exc.body.reason : "";
    if ((reason === "branch_moved" || reason === "head_unseen") && typeof (exc as ApiError).body.head === "string") {
      throw new HeadChanged((exc as ApiError).body.head as string, reason === "head_unseen");
    }
    throw exc;
  }
  if (challenge.launch.sha !== shown) throw new HeadChanged(challenge.launch.sha, !shown);
  const credential = await sign(challenge.options);
  return apiPost<Launched>("/api/deploys/launch", { challenge_id: challenge.challenge_id, credential });
}

/** The chip's colour class of a run's status. */
export function statusClass(status: DeployRun["status"] | undefined): string {
  switch (status) {
    case "success":
      return "ok";
    case "failure":
      return "crit";
    case "running":
    case "waiting":
      return "warn";
    default:
      return "off";
  }
}

/** The sign beside a status's colour: the page never tells a status by colour alone. */
export function statusMark(status: DeployRun["status"] | undefined): string {
  switch (status) {
    case "success":
      return "✓";
    case "failure":
      return "✗";
    case "running":
    case "waiting":
      return "…";
    default:
      return "–";
  }
}

export interface ProjectRow {
  id: string;
  name: string;
  envs: DeployEnv[];
  stale: boolean; // no good read of the project for two minutes
  error: string; // the reader's last error on the project
  okAt: string; // its last good read, '' never
  launch: boolean; // an environment of it has the owner's button: the panel keeps the button's column
}

/** One panel per project: its environments in the page's order and how its last read went (one read per project). */
export function projectRows(envs: DeployEnv[]): ProjectRow[] {
  return projectsOf(envs).map((p) => {
    const own = envs.filter((e) => e.project === p.id);
    return {
      ...p,
      envs: own,
      stale: own.some((e) => e.stale),
      error: own.find((e) => e.error)?.error ?? "",
      okAt: own[0]?.ok_at ?? "",
      launch: own.some((e) => e.launch),
    };
  });
}

export interface Delta {
  key: Key;
  params: Record<string, string | number>;
  tone: "acc" | "warn" | "muted" | "crit" | "ok";
}

/** What an environment says about its commits: how far what it runs is ahead of prod (`ahead_of`), and how many
 *  commits of its branch wait to be deployed there; prod itself says «all deployed» when none wait. */
export function deltas(row: DeployEnv): Delta[] {
  const out: Delta[] = [];
  if (row.ahead_of && row.ahead !== null) {
    out.push(row.ahead > 0
      ? { key: "deploys.ahead", params: { n: row.ahead, env: row.ahead_of }, tone: "acc" }
      : { key: "deploys.even", params: { env: row.ahead_of }, tone: "muted" });
  }
  if (row.pending !== null && row.pending > 0) {
    out.push({ key: "deploys.pending", params: { n: row.pending }, tone: "warn" });
  } else if (row.pending === 0 && !row.ahead_of) {
    out.push({ key: "deploys.up_to_date", params: {}, tone: "ok" });
  }
  // a listed commit that failed its checks is said in the row: the owner sees it before opening anything
  const failed = new Set([...(row.pending_commits ?? []), ...(row.ahead_commits ?? [])]
    .filter((c) => c.status === "failure").map((c) => c.sha));
  if (failed.size) out.push({ key: "deploys.checks_failed", params: { n: failed.size }, tone: "crit" });
  return out;
}

/** Everything is deployed there: the row says «all deployed» (nothing waits on the environment the others are
 *  measured against), its last run passed, no deploy is under way and the data is fresh — the row is painted green
 *  and its button goes grey. */
export function settled(row: DeployEnv): boolean {
  return row.pending === 0 && !row.ahead_of && row.run?.status === "success" && !row.in_flight && !row.stale;
}

/** A commit's checks (or one check) as a sign and a colour; null — none known. */
export function checkMark(state: CheckState | "" | undefined): { mark: string; tone: string } | null {
  if (state === "success") return { mark: "✓", tone: "ok" };
  if (state === "failure") return { mark: "✗", tone: "crit" };
  if (state === "pending") return { mark: "…", tone: "warn" };
  if (state === "warning") return { mark: "!", tone: "warn" };
  return null;
}

export interface CommitGroup {
  key: Key;
  params: Record<string, string | number>;
  commits: DeployCommit[];
  more: number; // commits the count has past the listed ones
}

/** The commits behind an environment's counts, as its open row lists them: what is not deployed there yet, and what
 *  it runs that prod does not. A count without its list (a reader of before the lists) lists nothing. */
export function commitGroups(row: DeployEnv): CommitGroup[] {
  const out: CommitGroup[] = [];
  const pending = row.pending_commits ?? [];
  if (row.pending && pending.length) {
    out.push({ key: "deploys.commits.pending", params: { env: row.env, n: row.pending }, commits: pending,
               more: Math.max(0, row.pending - pending.length) });
  }
  const ahead = row.ahead_commits ?? [];
  if (row.ahead && row.ahead_of && ahead.length) {
    out.push({ key: "deploys.commits.ahead", params: { env: row.env, ref: row.ahead_of, n: row.ahead },
               commits: ahead, more: Math.max(0, row.ahead - ahead.length) });
  }
  return out;
}

/** The run whose commit runs on the environment, when its last run is another one (failed, running). */
export function live(row: DeployEnv): DeployRun | null {
  const { run, deployed } = row;
  return deployed && run && deployed.sha !== run.sha ? deployed : null;
}

/** The journal shows its runs twenty at a time. */
export const JOURNAL_STEP = 20;

/** What the journal shows of `loaded` runs when `wanted` are asked for: how many, whether «more» is offered, and
 *  whether the server's next page must be read first. */
export function journalView(loaded: number, wanted: number, hasNext: boolean) {
  return { shown: Math.min(loaded, wanted), more: wanted < loaded || hasNext, fetch: wanted > loaded && hasNext };
}

export function shortSha(sha: string): string {
  return sha.slice(0, 7);
}

/** The journal's request: the filters set, the page before a moment. */
export function journalUrl(project: string, env: string, before = ""): string {
  const query = new URLSearchParams();
  if (project) query.set("project", project);
  if (project && env) query.set("env", env);
  if (before) query.set("before", before);
  const text = query.toString();
  return "/api/deploys/runs" + (text ? `?${text}` : "");
}

/** The projects of the filter, in the page's order, the platform last. */
export function projectsOf(envs: DeployEnv[]): { id: string; name: string }[] {
  const seen = new Map<string, string>();
  for (const e of envs) if (!seen.has(e.project)) seen.set(e.project, e.name);
  return [...seen].map(([id, name]) => ({ id, name }));
}

/** The environments of the filter for a project: its passport's, or the platform's release. */
export function envsOf(envs: DeployEnv[], project: string): string[] {
  if (project === PLATFORM) return [RELEASE_ENV];
  return envs.filter((e) => e.project === project).map((e) => e.env);
}

/** A dispatched run's inputs as one line: `skip_tests=true, confirm=deploy`. */
export function inputsText(inputs: Record<string, string>): string {
  return Object.entries(inputs)
    .map(([k, v]) => `${k}=${v}`)
    .join(", ");
}
