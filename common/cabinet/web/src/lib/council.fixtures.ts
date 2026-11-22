// A council as `GET /api/cards/<id>/councils` answers it, for the tests of the screens that show one.
import type { CouncilFinding, CouncilRound, CouncilRun, CouncilView } from "./types";

export function finding(over: Partial<CouncilFinding> = {}): CouncilFinding {
  return {
    id: "r1.f1",
    severity: "major",
    location: "AC 1",
    evidence: "the brief names no timeout",
    scenario: "a request hangs and the owner waits forever",
    recommendation: "name the timeout",
    ...over,
  };
}

export function run(over: Partial<CouncilRun> = {}): CouncilRun {
  return {
    n: 1,
    model: "claude:opus",
    state: "done",
    verdict: "ready",
    counts: { critical: 0, major: 0, minor: 0 },
    cost: 0,
    fail_reason: "",
    same_model: false,
    findings: [],
    questions: [],
    ...over,
  };
}

export function round(over: Partial<CouncilRound> = {}): CouncilRound {
  return { round: 1, version: 1, state: "ended", started: "2026-11-10T10:00:00Z", ended: "2026-11-10T10:05:00Z", runs: [], ...over };
}

/** A brief council whose first round ended on one critic's findings — a rejected major one, a minor one — and
 * waits for its writer. */
export function council(over: Partial<CouncilView> = {}): CouncilView {
  return {
    id: "c1",
    kind: "brief",
    card: "KD-5",
    state: "needs_writer",
    end_reason: "",
    round: 1,
    rounds: 3,
    created: "2026-11-10T10:00:00Z",
    ended: "",
    basis: { version: 1, scope_sha: "" },
    rounds_list: [
      round({
        runs: [
          run({
            verdict: "needs_changes",
            counts: { critical: 0, major: 2, minor: 1 },
            cost: 0.12,
            same_model: true,
            findings: [
              finding({ answer: "rejected", note: "by design" }),
              finding({ id: "r1.f2", location: "AC 2" }),
              finding({ id: "r1.f3", severity: "minor", location: "Format" }),
            ],
          }),
          run({ n: 2, model: "or-x" }),
        ],
      }),
    ],
    ...over,
  };
}
