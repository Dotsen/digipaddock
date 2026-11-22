// One card's page: the card, its decisions (with their views), saving with the card's revision.
// A 409 means someone changed the card meanwhile: say so and read it again; nothing is merged blindly.
import { ApiError, apiGet, apiPatch, apiPost } from "../api";
import { appendTaskLine, taskCounts, toggleTaskLine } from "../checklist";
import { errorText, t } from "../i18n.svelte";
import type { OverrideReason } from "../overrideReasons";
import { synced } from "../synced.svelte";
import { toast } from "../toast.svelte";
import type { Card, DecisionItem, JournalEvent } from "../types";
import { brief } from "./brief.svelte";

export function checklistCounts(card: Card): { ac: [number, number]; dod: [number, number] } {
  const ac = card.acceptance ?? [];
  return { ac: [ac.filter((a) => a.checked).length, ac.length], dod: taskCounts(card.dod ?? "") };
}

// every answer is applied only to the page it was asked for (the generation of load()) and
// never rolls the card back to an older revision; "saving" counts this generation's writes only.
class CardPage {
  id = $state<string | null>(null);
  card = $state<Card | null>(null);
  decisions = $state<Record<string, DecisionItem>>({});
  error = $state<ApiError | null>(null);
  private pending = $state(0);
  private gen = 0;
  private floor = 0; // the newest revision this generation has shown
  // the reads live events ask for: retried when they fail, run again on a resync of the app
  private readonly everything = synced(async () => {
    const gen = this.gen;
    await this.reloadAll();
    if (gen === this.gen) this.error = null; // a page that failed to open recovers on a resync
  });
  private readonly cardOnly = synced(() => this.reloadCard());

  get saving() {
    return this.pending > 0;
  }

  async load(id: string) {
    this.gen += 1;
    if (this.id !== id) {
      this.card = null;
      this.decisions = {};
    }
    this.id = id;
    this.error = null;
    this.pending = 0;
    this.floor = 0;
    const gen = this.gen;
    try {
      await this.reloadAll();
    } catch (exc) {
      if (exc instanceof ApiError && gen === this.gen) this.error = exc;
      throw exc;
    }
  }

  /** The page closed: answers still on their way are dropped, events and resyncs read nothing. */
  close() {
    this.gen += 1;
    this.id = null;
    this.pending = 0;
  }

  private reloadAll() {
    return Promise.all([this.reloadCard(), this.reloadDecisions()]).then(() => undefined);
  }

  async reloadCard() {
    const id = this.id;
    if (!id) return;
    const gen = this.gen;
    const card = await apiGet<Card>(`/api/cards/${encodeURIComponent(id)}`);
    if (gen === this.gen) this.apply(card);
  }

  async reloadDecisions() {
    const id = this.id;
    if (!id) return;
    const gen = this.gen;
    const items = await apiGet<DecisionItem[]>(`/api/decisions?state=all&card=${encodeURIComponent(id)}`);
    if (gen === this.gen && this.id === id) {
      this.decisions = Object.fromEntries(items.map((item) => [item.decision.id, item]));
    }
  }

  /** The card as the server answered, unless the page shows another card or this generation has already
   * seen a newer revision. */
  private apply(card: Card) {
    if (card.id !== this.id || card.revision < this.floor) return;
    this.floor = card.revision;
    this.card = card;
  }

  /** PATCH with the shown revision (or the one the edit was based on); true when saved. A conflict reads
   * the card again. */
  save(changes: Record<string, unknown>, revision?: number): Promise<boolean> {
    return this.write((card) =>
      apiPatch<Card>(`/api/cards/${encodeURIComponent(card.id)}`, { revision: revision ?? card.revision, changes }),
    );
  }

  /** One of the card's actions (the backend's list): the card, the person's comment and the agent's command are
   * one write; the run state shows how far the command got. A start sends the brief version it showed: a newer
   * one is refused (`brief_changed`) and the card and its brief are read again. */
  act(action: string, comment?: string, briefVersion?: number | null): Promise<boolean> {
    return this.write((card) =>
      apiPost<{ card: Card }>(`/api/cards/${encodeURIComponent(card.id)}/act`, {
        action,
        revision: card.revision,
        ...(comment !== undefined ? { comment } : {}),
        ...(typeof briefVersion === "number" ? { brief_version: briefVersion } : {}),
      }).then((answer) => answer.card),
    );
  }

  /** A manual status change, past the card's actions: only with the person's reason (a code). */
  override(status: string, reason: OverrideReason): Promise<boolean> {
    return this.write((card) =>
      apiPatch<Card>(`/api/cards/${encodeURIComponent(card.id)}`, {
        revision: card.revision,
        changes: { status },
        override_reason_code: reason.code,
        override_reason: reason.text,
        ...(reason.canonical ? { override_canonical: reason.canonical } : {}),
      }),
    );
  }

  private async write(request: (card: Card) => Promise<Card>): Promise<boolean> {
    const card = this.card;
    if (!card) return false;
    const gen = this.gen;
    this.pending += 1;
    try {
      const saved = await request(card);
      if (gen !== this.gen) return false; // the page moved on: this answer is not its card's any more
      this.apply(saved);
      await brief.refresh(card.id); // a write may change the brief (stale, approved) even with the socket down
      return true;
    } catch (exc) {
      if (gen !== this.gen) return false;
      if (exc instanceof ApiError && exc.status === 409) {
        toast(t(exc.body.reason === "brief_changed" ? "flow.reason.brief_changed" : "error.conflict"), "error");
        await Promise.all([this.reloadCard().catch(() => undefined), brief.refresh(card.id)]);
      } else {
        toast(errorText(exc), "error");
      }
      return false;
    } finally {
      if (gen === this.gen) this.pending -= 1;
    }
  }

  toggleAcceptance(index: number) {
    const items = (this.card?.acceptance ?? []).map((item, i) =>
      i === index ? { ...item, checked: !item.checked } : item,
    );
    return this.save({ acceptance: items });
  }

  addAcceptance(text: string) {
    const clean = text.trim();
    if (!clean || !this.card) return Promise.resolve(false);
    return this.save({ acceptance: [...this.card.acceptance, { text: clean, checked: false }] });
  }

  toggleDod(line: number) {
    return this.save({ dod: toggleTaskLine(this.card?.dod ?? "", line) });
  }

  addDod(label: string) {
    if (!label.trim()) return Promise.resolve(false);
    return this.save({ dod: appendTaskLine(this.card?.dod ?? "", label) });
  }

  async onEvent(event: JournalEvent) {
    if (!this.id || event.card !== this.id) return;
    // a changed card changes its decisions' views too (the revision a signature binds, "stale"); the card's next
    // step names its open decision, so a decision reads the card again too
    if (event.kind === "card.changed" || event.kind.startsWith("decision.")) await this.everything.run();
    // an agent's words after the person's question on its decision may be the answer
    else if (event.kind === "message.added" && this.answers(event)) await this.everything.run();
    // the card's buttons follow its brief, which follows the analyst's commands, runs and session (R17); its next
    // step follows the turns' outcomes and the problems too; a defer set or ended too
    else if (/^(brief|command|run|session|turn|problem)\./.test(event.kind) || /^card\.(un)?deferred$/.test(event.kind))
      await this.cardOnly.run();
  }

  private answers(event: JournalEvent): boolean {
    return (
      event.payload.kind === "text" &&
      !!event.actor?.startsWith("agent:") &&
      Object.values(this.decisions).some((d) => d.decision.state === "open" && d.view.asked_open)
    );
  }
}

export const cardPage = new CardPage();
