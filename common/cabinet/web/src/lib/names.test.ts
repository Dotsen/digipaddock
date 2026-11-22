import { describe, expect, it } from "vitest";
import { CANON, canonicalCard, rewriteNames } from "./names";

describe("names", () => {
  const acme = { commands: "acme", cards: "AC" };

  it("gives a catalog text the host's command and card names", () => {
    expect(rewriteNames("под root: kd-session-host resume <агент>; kd-cabinet operation request", acme)).toBe(
      "под root: acme-session-host resume <агент>; acme-cabinet operation request",
    );
    expect(rewriteNames("/usr/local/bin/kd-task, KD-<n>, KD-n, (KD-12)", acme)).toBe(
      "/usr/local/bin/acme-task, AC-<n>, AC-n, (KD-12)",
    );
    // the word placeholders too, as kd.naming.rewrite
    expect(rewriteNames("KD-target; KD-review, KD-id: KD-N; KD-name", acme)).toBe("AC-target; AC-review, AC-id: AC-N; KD-name");
  });

  it("keeps a file of the release tree and words that only contain the prefix", () => {
    expect(rewriteNames("common/bin/kd-task agents/dev/bin/kd-pr skd-x mykd-task kd-Task XKD-<n> KD-name", acme)).toBe(
      "common/bin/kd-task agents/dev/bin/kd-pr skd-x mykd-task kd-Task XKD-<n> KD-name",
    );
  });

  it("names a card of an earlier prefix by the current one (an old link)", () => {
    const moved = { ...CANON, aliases: ["OLD"] };
    expect(canonicalCard("OLD-9184", moved)).toBe("KD-9184");
    expect(canonicalCard("KD-9184", moved)).toBe("KD-9184");
    for (const other of ["XOLD-1", "OLD-01", "OLD-1234567", "AC-5", "OLD-x"]) expect(canonicalCard(other, moved)).toBe(other);
    expect(canonicalCard("OLD-5", CANON)).toBe("OLD-5");
  });

  it("is the identity on a host with the repository's names", () => {
    const text = "kd-task view KD-<n>";
    expect(rewriteNames(text, CANON)).toBe(text);
  });
});
