import type { AgentSession, Command, WorkerJob } from "../types";

// Test rows of the cabinet's commands and sessions (stores and components tests).
export function command(over: Partial<Command> = {}): Command {
  return {
    id: "c1",
    key: "ev:1:analyst",
    agent: "analyst",
    thread: 7,
    card: "KD-5",
    kind: "assigned",
    text: "[board] KD-5 assigned to you",
    initiator: "user:owner",
    state: "queued",
    attempts: 0,
    session: "",
    error: "",
    reason: "",
    created: "2026-10-17T10:00:00Z",
    updated: "2026-10-17T10:00:00Z",
    ...over,
  };
}

export function agentSession(over: Partial<AgentSession> = {}): AgentSession {
  return {
    id: "s1",
    thread: 7,
    agent: "analyst",
    runtime: "claude",
    slot: "",
    state: "working",
    error: "",
    started: "2026-10-17T10:00:05Z",
    ended: "",
    heartbeat: "2026-10-17T10:01:00Z",
    created: "2026-10-17T10:00:00Z",
    updated: "2026-10-17T10:01:00Z",
    usage: {},
    model: "",
    effort: "",
    ...over,
  };
}

export function workerJob(over: Partial<WorkerJob> = {}): WorkerJob {
  return {
    id: "j1",
    card: "KD-1",
    agent: "dev",
    spec: "implementer",
    skills: [],
    write: "repo",
    state: "queued",
    slot: "",
    reason: "",
    created: "2026-10-18T10:00:00Z",
    updated: "2026-10-18T10:00:00Z",
    started: "",
    ended: "",
    result: null,
    usage: null,
    ...over,
  };
}
