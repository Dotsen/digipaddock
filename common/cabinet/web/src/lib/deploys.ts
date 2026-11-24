// The «Deploys» page: the journal's address, its filters and how a run's status looks; the owner's prod launch.
import { apiPost } from "./api";
import type { DeployChallenge, DeployEnv, DeployRun } from "./types";
import { assertPasskey } from "./webauthn";

export const PLATFORM = "platform";
export const RELEASE_ENV = "release";
/** The word the owner types to start a prod deploy (the workflow's own «Type deploy to confirm»). */
export const CONFIRM_WORD = "deploy";

export interface Launched {
  state: string;
  sha: string;
  project: string;
  env: string;
  skip_tests: boolean;
}

/** The launch: a challenge over the environment's branch head now, the saved passkey's signature without a
 *  fingerprint or PIN (the server asks `userVerification: discouraged`), then kd-review dispatches it. */
export async function launchDeploy(
  row: Pick<DeployEnv, "project" | "env">,
  confirm: string,
  skipTests: boolean,
  sign: (options: Record<string, unknown>) => Promise<unknown> = assertPasskey,
): Promise<Launched> {
  const challenge = await apiPost<DeployChallenge>("/api/deploys/launch/challenge", {
    project: row.project,
    env: row.env,
    confirm,
    skip_tests: skipTests,
  });
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

/** One row per project: its environments in the page's order; stale — any of them is. */
export function projectRows(envs: DeployEnv[]): { id: string; name: string; envs: DeployEnv[]; stale: boolean }[] {
  return projectsOf(envs).map((p) => {
    const own = envs.filter((e) => e.project === p.id);
    return { ...p, envs: own, stale: own.some((e) => e.stale) };
  });
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
