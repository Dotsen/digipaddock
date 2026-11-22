// One card's runs: what was asked of its agents (commands), their sessions in its thread and its
// worker jobs. Live command, run, session and job events of the card read them again; a read of a
// card the page has left is dropped (the generation); a failed read is retried (the stale strip).
import { apiGet } from "../api";
import { synced } from "../synced.svelte";
import type { AgentSession, Command, JournalEvent, WorkerJob } from "../types";

const RUN_EVENTS = /^(command|run|session|job)\./;
const path = (card: string, what: string) => `/api/cards/${encodeURIComponent(card)}/${what}`;

class CardRuns {
  card = $state<string | null>(null);
  commands = $state<Command[]>([]);
  sessions = $state<AgentSession[]>([]);
  jobs = $state<WorkerJob[]>([]);
  private gen = 0;
  private readonly sync = synced(() => this.read());

  get stale() {
    return this.sync.stale;
  }

  /** A session on the card starts or works: its heartbeat changes with no event, so the page reads it again. */
  get live(): boolean {
    return this.sessions.some((s) => s.state === "starting" || s.state === "working");
  }

  load(card: string): Promise<void> {
    this.gen += 1;
    this.card = card;
    this.commands = [];
    this.sessions = [];
    this.jobs = [];
    return this.sync.run();
  }

  close() {
    this.gen += 1;
    this.card = null;
  }

  refresh(): Promise<void> {
    return this.sync.run();
  }

  async onEvent(event: JournalEvent) {
    if (!this.card || event.card !== this.card || !RUN_EVENTS.test(event.kind)) return;
    await this.refresh();
  }

  private async read() {
    const card = this.card;
    if (!card) return;
    const gen = this.gen;
    const [commands, sessions, jobs] = await Promise.all([
      apiGet<Command[]>(path(card, "commands")),
      apiGet<AgentSession[]>(path(card, "sessions")),
      apiGet<WorkerJob[]>(path(card, "jobs")),
    ]);
    if (gen !== this.gen) return; // the page moved to another card meanwhile
    this.commands = commands;
    this.sessions = sessions;
    this.jobs = jobs;
  }
}

export const runs = new CardRuns();
