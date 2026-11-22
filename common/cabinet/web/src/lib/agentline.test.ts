import { describe, expect, it } from "vitest";
import { namedCards, statusLine } from "./agentline";
import type { AgentFacts } from "./types";

const facts = (over: Partial<AgentFacts> = {}): AgentFacts => ({
  working: 0, waiting_you: 0, queued: 0, failed: 0, stale: 0, questions: [], failures: [], heartbeat: "",
  last_action: null, progress: "", ...over,
});

describe("the coordinator's status line (KD-9043)", () => {
  it("says what it does in words", () => {
    expect(statusLine("free", facts(), "boss").key).toBe("agent.line.ready");
    expect(statusLine("working", facts(), "boss").key).toBe("agent.line.working");
    expect(statusLine("waiting_you", facts(), "boss").key).toBe("agent.line.waiting");
    const failed = statusLine("error", facts({ failures: [{ card: "", title: "", reason: "лимит", at: "" }] }), "boss");
    expect([failed.key, failed.params.reason, failed.tone]).toEqual(["agent.line.error", "лимит", "bad"]);
  });

  it("a quiet heartbeat is a lost contact, not work", () => {
    const line = statusLine("working", facts({ stale: 1 }), "boss");
    expect([line.key, line.params.name]).toEqual(["agent.line.lost", "BOSS"]);
    expect(statusLine("unavailable", facts(), "boss").key).toBe("agent.line.lost");
  });

  it("finds the cards a chat names", () => {
    expect(namedCards(["Голосовые уже есть в KD-9032, файлы — KD-9041.", "Снова KD-9032", "XKD-1 KD-0"])).toEqual([
      "KD-9032", "KD-9041"]);
  });

  it("finds them by the host's card prefix", () => {
    expect(namedCards(["AC-7 and KD-8"], "AC")).toEqual(["AC-7"]);
  });
});
