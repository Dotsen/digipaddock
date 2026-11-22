import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentSession, Command, WorkerJob } from "../types";
import { agentSession, command, workerJob } from "./runs.fixtures";
import { runs } from "./sessions.svelte";

function json(body: unknown) {
  return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
}

function serve(commands: Command[], sessions: AgentSession[], jobs: WorkerJob[] = []) {
  return vi.fn((url: string) =>
    Promise.resolve(json(url.endsWith("/commands") ? commands : url.endsWith("/jobs") ? jobs : sessions)),
  );
}

afterEach(() => {
  runs.close();
  vi.unstubAllGlobals();
});

describe("runs", () => {
  it("loads the card's commands, sessions and worker jobs", async () => {
    const fetch = serve([command()], [agentSession()], [workerJob()]);
    vi.stubGlobal("fetch", fetch);
    await runs.load("KD-5");
    expect(fetch.mock.calls.map((c) => c[0]).sort()).toEqual([
      "/api/cards/KD-5/commands",
      "/api/cards/KD-5/jobs",
      "/api/cards/KD-5/sessions",
    ]);
    expect(runs.commands.map((c) => c.id)).toEqual(["c1"]);
    expect(runs.sessions.map((s) => s.id)).toEqual(["s1"]);
    expect(runs.jobs.map((j) => j.id)).toEqual(["j1"]);
  });

  it("is live while a session on the card starts or works: the page reads its heartbeat again (KD-9036)", async () => {
    vi.stubGlobal("fetch", serve([], [agentSession({ state: "idle" })]));
    await runs.load("KD-5");
    expect(runs.live).toBe(false);
    for (const state of ["starting", "working"] as const) {
      vi.stubGlobal("fetch", serve([], [agentSession({ state: "idle" }), agentSession({ id: "s2", state })]));
      await runs.refresh();
      expect(runs.live).toBe(true);
    }
  });

  it("live command, run, session and job events of its card refresh it; others do not", async () => {
    vi.stubGlobal("fetch", serve([command()], []));
    await runs.load("KD-5");
    const fetch = serve([command({ state: "started" })], [agentSession()], [workerJob({ state: "running" })]);
    vi.stubGlobal("fetch", fetch);
    await runs.onEvent({ seq: 9, kind: "command.delivered", card: "KD-6", payload: {} });
    await runs.onEvent({ seq: 10, kind: "message.added", card: "KD-5", payload: {} });
    expect(fetch).not.toHaveBeenCalled();
    await runs.onEvent({ seq: 11, kind: "run.started", card: "KD-5", payload: {} });
    expect(runs.commands[0].state).toBe("started");
    await runs.onEvent({ seq: 12, kind: "session.changed", card: "KD-5", payload: {} });
    await runs.onEvent({ seq: 13, kind: "job.started", card: "KD-5", payload: {} });
    expect(runs.jobs[0].state).toBe("running");
    expect(fetch).toHaveBeenCalledTimes(9);
  });

  it("drops a late reply for the card the page has left", async () => {
    const pending: ((r: Response) => void)[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        url.includes("KD-5") ? new Promise<Response>((r) => pending.push(r)) : Promise.resolve(json([])),
      ),
    );
    const old = runs.load("KD-5");
    const now = runs.load("KD-6");
    pending.forEach((answer) => answer(json([command()])));
    await Promise.all([old, now]);
    expect(runs.card).toBe("KD-6");
    expect(runs.commands).toEqual([]);
  });
});
