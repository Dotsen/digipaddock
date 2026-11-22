// owner/repo as the cabinet takes it for a project's repository (kd.cabinet.projects_api.REPO): Latin
// letters, digits, "_", "." and "-", each part from a letter or a digit, no "..". The form checks it before any
// request; the server checks it again.
export const REPO_PATTERN = "[A-Za-z0-9][A-Za-z0-9_.\\-]{0,63}/[A-Za-z0-9][A-Za-z0-9_.\\-]{0,99}";
const REPO = new RegExp(`^${REPO_PATTERN}$`);

export function validRepo(value: string): boolean {
  return REPO.test(value) && !value.includes("..");
}

/** A merge effect of the project's passport that reaches prod: red in the PR's block, «prod» on the button. */
export function prodEffect(effect: string | undefined): boolean {
  return /prod/i.test(effect ?? "");
}

/** A request the root unit has not answered yet: the project's repository waits. */
export function repoWaiting(state: string | undefined): boolean {
  return state === "pending" || state === "unbinding";
}
