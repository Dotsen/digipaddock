// The card's brief (R17): its state, versions and what changed, as the backend derives them. Every live
// event of the card reads it again (a failed read is retried and keeps the last view); an answer for another
// card, or for a page that moved on, is dropped.
import { apiGet } from "../api";
import { isEcho } from "../events";
import { t, type Key } from "../i18n.svelte";
import { synced } from "../synced.svelte";
import type { BriefState, BriefView, JournalEvent } from "../types";

/** The brief's state in words, as the card's indicator and the tab show it. */
export function briefLabel(brief: BriefState): string {
  const version = { version: brief.version };
  switch (brief.state) {
    case "waiting_user":
      return t("brief.state.waiting_user", { n: brief.questions });
    case "ready":
      if (brief.legacy) return t("brief.state.ready_legacy");
      return t(brief.approved ? "brief.state.ready_approved" : "brief.state.ready", version);
    case "stale":
      return brief.approved ? t("brief.state.stale_approved") : t("brief.state.stale", version);
    case "in_council":
      return t("brief.state.in_council", version);
    default:
      return t(`brief.state.${brief.state}` as Key);
  }
}

/** The indicator's colour: a help beside the words, never instead of them. */
export function briefTone(brief: BriefState): "good" | "info" | "warn" | "crit" | "" {
  if (brief.state === "ready") return brief.approved ? "good" : brief.legacy ? "warn" : "info";
  if (brief.state === "stale") return "warn";
  if (brief.state === "failed") return "crit";
  if (brief.state === "waiting_user" || brief.state === "in_council") return "info";
  return "";
}

class BriefStore {
  id = $state<string | null>(null);
  view = $state<BriefView | null>(null);
  private gen = 0;
  private readonly data = synced(() => this.reload());

  /** The last read failed and is retried; the view shown is the one before. */
  get stale() {
    return this.data.stale;
  }

  get error() {
    return this.data.error;
  }

  load(id: string): Promise<void> {
    this.gen += 1;
    if (this.id !== id) this.view = null;
    this.id = id;
    return this.data.run();
  }

  /** The page closed: answers on their way are dropped, events and resyncs read nothing. */
  close() {
    this.gen += 1;
    this.id = null;
  }

  /** Read the shown card's brief again (a start refused because the brief changed). */
  refresh(id: string): Promise<void> {
    return this.id === id ? this.data.run() : Promise.resolve();
  }

  private async reload() {
    const id = this.id;
    if (!id) return;
    const gen = this.gen;
    const view = await apiGet<BriefView>(`/api/cards/${encodeURIComponent(id)}/brief`);
    if (gen === this.gen && this.id === id) this.view = view;
  }

  async onEvent(event: JournalEvent) {
    if (!this.id || event.card !== this.id || isEcho(event)) return;
    await this.data.run();
  }
}

export const brief = new BriefStore();
