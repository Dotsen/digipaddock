// Agents for the tests: free, hosted, nothing on them unless given.
import type { AgentDetail, AgentFacts, AgentSummary } from "./types";

export function facts(over: Partial<AgentFacts> = {}): AgentFacts {
  return {
    working: 0, waiting_you: 0, queued: 0, failed: 0, stale: 0, questions: [], failures: [],
    heartbeat: "", last_action: null, progress: "", ...over,
  };
}

export function agentSummary(over: Partial<AgentSummary> = {}): AgentSummary {
  return {
    name: "dev", state: "free", facts: facts(), cards: { inbox: 0, to_do: 0, in_progress: 0, review: 0, blocked: 0 },
    specs: [], usage_today: null, host: { working: 0, queued: 0, slots: 2 }, ...over,
  };
}

export function agentDetail(over: Partial<AgentDetail> = {}): AgentDetail {
  const { cards: _cards, ...rest } = agentSummary();
  return { ...rest, counts: { inbox: 0, to_do: 0, in_progress: 0, review: 0, blocked: 0 }, cards: [], progress: [], sessions: [], ...over };
}
