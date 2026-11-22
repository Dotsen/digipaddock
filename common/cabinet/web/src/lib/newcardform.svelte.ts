// The new card's form is the app's, not the board's: the menu opens it from any page, the board from
// its header. The form's lists (the parent, the dependencies, the labels) offer every card the person sees, so
// the cards are read here — not taken from the board, which holds one project or one search at a time.
import { apiGet } from "./api";
import { compact, type CardCompact } from "./types";

class NewCardForm {
  open = $state(false);
  /** Where the form opens for someone with no project of their own yet: the board's project filter. */
  project = $state("");
  cards = $state.raw<CardCompact[]>([]);
  private asked = 0;

  show(project = "") {
    this.project = project;
    this.open = true;
    void this.read();
  }

  close() {
    this.open = false;
  }

  /** A sign-out: nothing of one person's board is offered to the next. */
  reset() {
    this.asked++;
    this.open = false;
    this.project = "";
    this.cards = [];
  }

  private async read() {
    const asked = ++this.asked;
    try {
      const found = await apiGet<CardCompact[]>("/api/cards");
      if (asked === this.asked) this.cards = found.map(compact);
    } catch {
      // the lists offer what was read before; the form itself works without them
    }
  }
}

export const newCardForm = new NewCardForm();
