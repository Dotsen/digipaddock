// The person's projects (with each one's route agent) and whom a card can be given to, read once per sign-in. A project's
// name or route changed anywhere (the Projects section, another device) reads the list again in place, so every
// screen shows the new name without a reload.
import { apiGet } from "../api";
import { onLiveEvent, onResync } from "../live.svelte";
import type { Project } from "../types";

class Projects {
  list = $state<Project[]>([]);
  /** Whom a card can be given to (/api/assignees): the routes' agents, the owner, the coordinator. */
  assignees = $state<string[]>([]);
  private pending: Promise<void> | null = null;
  private asked = 0; // each read of the list takes the next number
  private shown = 0; // the number of the read the list shows: an older answer arriving later is dropped

  /** The agents a message can mention: every project's route, and the coordinator. */
  get agents(): string[] {
    return [...new Set(["@boss", ...this.list.map((p) => p.route).filter((r) => r.startsWith("@"))])].sort();
  }

  /** A project's name for the screens; its id while the list is not read yet (or for a project now gone). */
  name(id: string): string {
    return this.list.find((p) => p.id === id)?.name ?? id;
  }

  private show(read: number, list: Project[], assignees: string[]) {
    if (read <= this.shown) return;
    this.shown = read;
    this.list = list;
    this.assignees = assignees;
  }

  ensure(): Promise<void> {
    if (this.pending) return this.pending;
    const read = ++this.asked;
    const pending: Promise<void> = Promise.all([
      apiGet<Project[]>("/api/projects"),
      apiGet<string[]>("/api/assignees").catch(() => []),
    ]).then(
      ([list, assignees]) => {
        if (this.pending !== pending) return; // signed out meanwhile
        this.show(read, list, assignees);
      },
      (exc) => {
        if (this.pending === pending) this.pending = null;
        throw exc;
      },
    );
    this.pending = pending;
    return pending;
  }

  /** Read the projects (and whom cards go to) again after a change; nothing before the first read. */
  async refresh(): Promise<void> {
    const pending = this.pending;
    if (!pending) return;
    const read = ++this.asked;
    const [list, assignees] = await Promise.all([
      apiGet<Project[]>("/api/projects"),
      apiGet<string[]>("/api/assignees").catch(() => this.assignees),
    ]);
    if (this.pending === pending) this.show(read, list, assignees); // not after a sign-out
  }

  /** Keep the list current while signed in: a project event or a resync reads it again; returns the stop. */
  follow(): () => void {
    const again = () => void this.refresh().catch(() => undefined);
    const stopEvents = onLiveEvent((event) => {
      if (event.kind.startsWith("project.")) again();
    });
    const stopResync = onResync(again);
    return () => {
      stopEvents();
      stopResync();
    };
  }

  reset() {
    this.pending = null;
    this.shown = this.asked; // what was asked before the sign-out is not shown after it
    this.list = [];
    this.assignees = [];
  }
}

export const projects = new Projects();
