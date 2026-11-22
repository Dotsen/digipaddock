// the council's composition on the screens — which one is in force and how a chip names it.
import { beforeEach, expect, it } from "vitest";
import { answerState, bySeverity, effective, outcomes, same, split, summary, totals } from "./council";
import { finding, round, run } from "./council.fixtures";
import { setLocale } from "./i18n.svelte";

beforeEach(() => setLocale("ru"));

const BRIEF = { critics: ["claude:opus", "or-x"], rounds: 3 };
const CODE = { critics: ["claude:opus"], rounds: 3, min_lines: 100 };

it("a kind the card names wins over its project's; a kind without critics is no council", () => {
  expect(effective({}, { brief: BRIEF, code: CODE })).toEqual({ brief: BRIEF, code: CODE });
  expect(effective({ brief: { critics: [], rounds: 3 } }, { brief: BRIEF, code: CODE })).toEqual({ code: CODE });
  const own = { critics: ["or-x"], rounds: 2 };
  expect(effective({ brief: own }, { brief: BRIEF, code: CODE })).toEqual({ brief: own, code: CODE });
  expect(effective({}, {})).toEqual({});
});

it("the summary counts the critics by kind", () => {
  expect(summary({})).toBe("нет");
  expect(summary({ brief: { critics: ["a", "b"], rounds: 3 }, code: { critics: ["a"], rounds: 3, min_lines: 100 } })).toBe("ТЗ: 2 · код: 1");
  expect(summary({ code: CODE })).toBe("код: 1");
  expect(summary({ brief: { critics: [], rounds: 3 } })).toBe("нет");
});

it("two compositions are the same by their kinds, critics in order, rounds and threshold", () => {
  expect(same({}, {})).toBe(true);
  expect(same({ code: CODE }, { code: { min_lines: 100, rounds: 3, critics: ["claude:opus"] } })).toBe(true);
  expect(same({ brief: BRIEF }, { brief: { ...BRIEF, critics: ["or-x", "claude:opus"] } })).toBe(false);
  expect(same({ brief: BRIEF }, { brief: { ...BRIEF, rounds: 2 } })).toBe(false);
  expect(same({ brief: BRIEF }, {})).toBe(false);
  expect(same({ code: CODE }, { code: { ...CODE, min_lines: 0 } })).toBe(false);
});

// the block lists a critic's findings by weight, says how each stands with its writer, and keeps the
// critic's questions apart from the inputs it had no access to
it("a critic's findings are listed the gravest first, each weight in the critic's own order", () => {
  const list = [
    finding({ id: "f1", severity: "minor" }), finding({ id: "f2", severity: "critical" }),
    finding({ id: "f3" }), finding({ id: "f4", severity: "critical" }),
  ];
  expect(bySeverity(list).map((f) => f.id)).toEqual(["f2", "f4", "f3", "f1"]);
  expect(list.map((f) => f.id)).toEqual(["f1", "f2", "f3", "f4"]); // the view's own list stays as it came
});

it("how a finding stands with its writer: the answer, none where one is due, optional for a minor one", () => {
  expect(answerState(finding({ answer: "fixed" }))).toBe("fixed");
  expect(answerState(finding({ severity: "minor", answer: "rejected" }))).toBe("rejected");
  expect(answerState(finding())).toBe("none");
  expect(answerState(finding({ severity: "critical" }))).toBe("none");
  expect(answerState(finding({ severity: "minor" }))).toBe("optional");
});

it("a critic's questions stand apart from the inputs it had no access to", () => {
  const asked = { text: "Which timezone?", options: ["UTC"] };
  const missing = { text: "the log format", missing: true };
  expect(split([missing, asked])).toEqual({ asked: [asked], missing: [missing] });
  expect(split([])).toEqual({ asked: [], missing: [] });
});

it("a round counts its critics' findings by weight", () => {
  const entry = round({ runs: [
    run({ counts: { critical: 1, major: 2, minor: 0 } }), run({ n: 2, counts: { critical: 0, major: 1, minor: 3 } }),
  ] });
  expect(totals(entry)).toEqual({ critical: 1, major: 3, minor: 3 });
});

// the writer answers a round's findings on those findings, so the round's own line says what became of
// them
it("a round counts how its findings stand with their writer; a minor one without an answer is in no count", () => {
  const entry = round({ runs: [
    run({ findings: [
      finding({ answer: "fixed" }), finding({ id: "r1.f2" }), finding({ id: "r1.f3", severity: "minor" }),
    ] }),
    run({ n: 2, findings: [
      finding({ id: "r1.f4", answer: "fixed" }), finding({ id: "r1.f5", severity: "critical", answer: "rejected" }),
      finding({ id: "r1.f6", severity: "minor", answer: "later" }),
    ] }),
  ] });
  expect(outcomes(entry)).toEqual({ fixed: 2, rejected: 1, later: 1, none: 1 });
  expect(outcomes(round())).toEqual({ fixed: 0, rejected: 0, later: 0, none: 0 });
});
