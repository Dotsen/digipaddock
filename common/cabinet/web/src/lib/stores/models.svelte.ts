// The cabinet's models of other providers, read once per sign-in for the card's chips, the projects'
// defaults and the Settings section; a model event anywhere (the section, another device, the host's probe)
// reads the list again in place.
import { apiGet } from "../api";
import { t } from "../i18n.svelte";
import { onLiveEvent, onResync } from "../live.svelte";
import { claudeVersion } from "../modelversion";
import type { LlmModel } from "../types";

class Models {
  list = $state<LlmModel[]>([]);
  private pending: Promise<void> | null = null;
  private asked = 0; // each read of the list takes the next number
  private shown = 0; // the number of the read the list shows: an older answer arriving later is dropped

  /** A model's title where a model is chosen or named: one of the subscription says so, with the
   * version its alias served last — "Claude Opus 5.5 · subscription"; a name that reads as no number is
   * shown as it is. */
  title(model: LlmModel): string {
    const title = model.title || model.id;
    if (!model.builtin) return title;
    const version = model.version ? claudeVersion(model.version) || `(${model.version})` : "";
    return t("models.builtin", { title: version ? `${title} ${version}` : title });
  }

  /** The models that served a turn or a critic's job in a few words: the first — the costliest; a
   * cabinet model's id reads as its title — and how many more. */
  served(names: string[]): string {
    if (!names.length) return "";
    const own = this.list.find((m) => !m.builtin && m.id === names[0]);
    return (own ? this.title(own) : names[0]) + (names.length > 1 ? ` +${names.length - 1}` : "");
  }

  /** A model's title for the screens; its id while the list is not read yet (or for a model now gone). */
  name(id: string): string {
    const found = this.list.find((m) => m.id === id);
    return found ? this.title(found) : id;
  }

  private show(read: number, list: LlmModel[]) {
    if (read <= this.shown) return;
    this.shown = read;
    this.list = list;
  }

  ensure(): Promise<void> {
    if (this.pending) return this.pending;
    const read = ++this.asked;
    const pending: Promise<void> = apiGet<LlmModel[]>("/api/models").then(
      (list) => {
        if (this.pending !== pending) return; // signed out meanwhile
        this.show(read, list);
      },
      (exc) => {
        if (this.pending === pending) this.pending = null;
        throw exc;
      },
    );
    this.pending = pending;
    return pending;
  }

  /** Read the models again after a change; nothing before the first read. */
  async refresh(): Promise<void> {
    const pending = this.pending;
    if (!pending) return;
    const read = ++this.asked;
    const list = await apiGet<LlmModel[]>("/api/models");
    if (this.pending === pending) this.show(read, list); // not after a sign-out
  }

  /** One model as the API just answered it (a change, a probe request): shown in place, ahead of the next read. */
  put(model: LlmModel) {
    const at = this.list.findIndex((m) => m.id === model.id);
    if (at >= 0) {
      this.list = this.list.map((m, i) => (i === at ? model : m));
      return;
    }
    // a new one: after the subscription's (they come first, in the catalog's order), among the rest by id
    const own = [...this.list.filter((m) => !m.builtin), model].sort((a, b) => a.id.localeCompare(b.id));
    this.list = [...this.list.filter((m) => m.builtin), ...own];
  }

  /** Keep the list current while signed in: a model event or a resync reads it again; returns the stop. */
  follow(): () => void {
    const again = () => void this.refresh().catch(() => undefined);
    const stopEvents = onLiveEvent((event) => {
      if (event.kind.startsWith("llm_model.") || event.kind === "llm_key.changed") again();
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
  }
}

export const models = new Models();
