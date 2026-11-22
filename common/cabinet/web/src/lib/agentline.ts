// The coordinator's one status line over its chat: what it does in words, never a machine status, slot
// counts or a heartbeat's age. A lost heartbeat is "connection lost", not an endless spinner.
import type { AgentFacts, AgentState } from "./types";

export interface StatusLine {
  key: "agent.line.ready" | "agent.line.working" | "agent.line.waiting" | "agent.line.queued" | "agent.line.error" |
    "agent.line.lost" | "agent.line.unknown";
  params: Record<string, string>;
  tone: "calm" | "busy" | "attention" | "bad";
}

export function statusLine(state: AgentState, facts: AgentFacts, name: string): StatusLine {
  const who = name.toUpperCase();
  if (facts.stale > 0 || state === "unavailable") return { key: "agent.line.lost", params: { name: who }, tone: "bad" };
  switch (state) {
    case "working":
      return { key: "agent.line.working", params: {}, tone: "busy" };
    case "waiting_you":
      return { key: "agent.line.waiting", params: {}, tone: "attention" };
    case "queued":
      return { key: "agent.line.queued", params: {}, tone: "busy" };
    case "error":
      return { key: "agent.line.error", params: { reason: facts.failures[0]?.reason ?? "" }, tone: "bad" };
    case "free":
      return { key: "agent.line.ready", params: {}, tone: "calm" };
    default:
      return { key: "agent.line.unknown", params: {}, tone: "calm" };
  }
}

/** Card ids a chat's messages name (KD-n): the panel "Tasks" shows them with their reason. */
export function namedCards(bodies: string[], prefix = "KD"): string[] {
  const found = new Set<string>();
  const pattern = new RegExp(`\\b${prefix}-[1-9][0-9]{0,5}\\b`, "g");
  for (const body of bodies) for (const match of body.matchAll(pattern)) found.add(match[0]);
  return [...found];
}
