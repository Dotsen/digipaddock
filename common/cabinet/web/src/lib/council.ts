// The council of models on the screens: the composition a card or a project holds (kd.council — per kind
// the critics, the rounds and, for the code, the least diff), which one is in force on a card, and how a chip
// names it. The limits are those of common/coordination/council.json; the server checks them again.
import { hasKey, t, type Key } from "./i18n.svelte";
import type {
  Council, CouncilFacts, CouncilFinding, CouncilKind, CouncilPart, CouncilQuestion, CouncilRound, CouncilView,
} from "./types";

export type { Council, CouncilKind, CouncilPart };

export const KINDS: readonly CouncilKind[] = ["brief", "code"];
export const CRITICS_MAX = 10;
export const ROUNDS_DEFAULT = 3;
export const ROUNDS_MAX = 5;
export const MIN_LINES_DEFAULT = 100;
export const MIN_LINES_MAX = 100000;

/** The composition in force on a card: per kind the card's when it names the kind, else its project's; a kind
 * left without critics is not there. */
export function effective(card: Council, project: Council): Council {
  const out: Council = {};
  for (const kind of KINDS) {
    const part = kind in card ? card[kind] : project[kind];
    if (part?.critics.length) out[kind] = part;
  }
  return out;
}

/** "brief: 2 · code: 1" — how many critics read each kind; "none" without a council. */
export function summary(council: Council): string {
  const parts = KINDS.filter((kind) => council[kind]?.critics.length).map(
    (kind) => `${t(`council.${kind}`)}: ${council[kind]!.critics.length}`,
  );
  return parts.length ? parts.join(" · ") : t("council.none");
}

/** Whether two compositions say the same: the kinds, the critics in their order, the rounds, the threshold. */
export function same(a: Council, b: Council): boolean {
  return KINDS.every((kind) => {
    const x = a[kind];
    const y = b[kind];
    if (!x || !y) return !x && !y;
    return (
      x.rounds === y.rounds && (x.min_lines ?? null) === (y.min_lines ?? null) &&
      x.critics.length === y.critics.length && x.critics.every((critic, i) => critic === y.critics[i])
    );
  });
}

// --- a gathering: how it stands, in the words the block, the brief tab and the card share ----------

/** How a gathering stands: its state, the critics of its round that ended, the findings left to its writer. */
export type Standing = Pick<CouncilFacts, "state" | "round" | "rounds" | "done" | "total" | "open_findings" | "end_reason">;

/** A gathering still under way: its critics read, or its writer answers. */
export const isOpen = (council: { state: string }) => council.state === "running" || council.state === "needs_writer";

/** A finding that holds its writer's handoff: a critical or a major one without an answer. A minor one is
 * answered too but holds and prolongs nothing. */
export const owed = (finding: CouncilFinding) => finding.severity !== "minor" && !finding.answer;

// --- what the block lists ---------------------------------------------------------------------------

export const SEVERITIES = ["critical", "major", "minor"] as const;
export type Severity = (typeof SEVERITIES)[number];

/** A critic's findings as its card lists them: the gravest first, each weight in the critic's own order. */
export const bySeverity = (findings: CouncilFinding[]) =>
  SEVERITIES.flatMap((severity) => findings.filter((finding) => finding.severity === severity));

/** What a finding's row marks and a round's line counts: the answer its writer gave, or `none` — a
 * critical or a major finding still without one. */
export const OUTCOMES = ["fixed", "rejected", "later", "none"] as const;
export type Outcome = (typeof OUTCOMES)[number];

/** How a finding stands with its writer: an outcome, or `optional` — a minor one without an answer, which holds
 * nothing and so carries no mark. */
export type AnswerState = Outcome | "optional";
export const answerState = (finding: CouncilFinding): AnswerState =>
  finding.answer ?? (owed(finding) ? "none" : "optional");

/** A critic's questions apart from the inputs it had no access to. */
export function split(questions: CouncilQuestion[]): { asked: CouncilQuestion[]; missing: CouncilQuestion[] } {
  return { asked: questions.filter((q) => !q.missing), missing: questions.filter((q) => q.missing) };
}

/** What a round's critics found, by weight. */
export function totals(entry: CouncilRound): Record<Severity, number> {
  const sum = { critical: 0, major: 0, minor: 0 };
  for (const run of entry.runs) for (const severity of SEVERITIES) sum[severity] += run.counts[severity];
  return sum;
}

/** What became of a round's findings: the writer answers a round on its own findings, so the count is the
 * round's. A minor finding without an answer is in none of them. */
export function outcomes(entry: CouncilRound): Record<Outcome, number> {
  const sum = { fixed: 0, rejected: 0, later: 0, none: 0 };
  for (const run of entry.runs) {
    for (const finding of run.findings) {
      const state = answerState(finding);
      if (state !== "optional") sum[state] += 1;
    }
  }
  return sum;
}

const RUN_ENDED = new Set(["done", "failed", "cancelled"]);

/** The standing of a gathering as its block has it: counted over the runs of its current round. */
export function standing(view: CouncilView): Standing {
  const runs = view.rounds_list.find((entry) => entry.round === view.round)?.runs ?? [];
  return {
    state: view.state,
    round: view.round,
    rounds: view.rounds,
    end_reason: view.end_reason,
    done: runs.filter((run) => RUN_ENDED.has(run.state)).length,
    total: runs.length,
    open_findings: runs.flatMap((run) => run.findings).filter(owed).length,
  };
}

/** Who writes what a council of this kind reads: the analyst the brief. */
export const writerOf = (kind: CouncilKind, assignee = "") => (kind === "brief" ? "@analyst" : assignee);

/** "critics reading: 1 of 2", "waits for @analyst's answers: 2", "clean", "cancelled · by the owner". Its findings
 * answered, a brief's council waits for a new version, the code's for a new commit. */
export function stateText(council: Standing, writer: string, kind: CouncilKind = "brief"): string {
  if (council.state === "running") return t("council.state.running", { done: council.done, total: council.total });
  if (council.state === "needs_writer") {
    if (council.open_findings) return t("council.state.needs_writer", { agent: writer, n: council.open_findings });
    return t(kind === "code" ? "council.state.needs_head" : "council.state.needs_version", { agent: writer });
  }
  const whole = `council.state.${council.state}.${council.end_reason}`;
  if (hasKey(whole)) return t(whole); // an end the state's own words would misname
  const said = t(`council.state.${council.state}` as Key);
  const reason = `council.reason.${council.end_reason}`;
  return hasKey(reason) ? `${said} · ${t(reason)}` : said;
}

/** A critic's run in a word: its verdict once it read, else where it stands ("queued", "did not answer"). */
export function runText(run: { state: string; verdict: string }): string {
  const key = run.state === "done" && run.verdict ? `council.verdict.${run.verdict}` : `council.run.${run.state}`;
  return hasKey(key) ? t(key) : run.verdict || run.state;
}
