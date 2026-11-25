// The menu's search: a card's number (KD-9443, 9443, #9443) opens that card when the search finds one card of
// that number; anything else — or a number that names none, or several — opens the board searched.
import { apiGet } from "./api";
import type { CardCompact } from "./types";

// the store's card id (CARD_ID): a prefix of up to 8 letters and digits, a number of up to six digits
const NUMBER = /^(?:([A-Za-z][A-Za-z0-9]{0,7})-|#)?([1-9][0-9]{0,5})$/;

/** A card's number as typed: `key` — the full id when one was typed (upper case), "" for a bare number. */
export function cardNumber(text: string): { n: number; key: string } | null {
  const match = NUMBER.exec(text.trim());
  return match ? { n: Number(match[2]), key: match[1] ? `${match[1].toUpperCase()}-${match[2]}` : "" } : null;
}

/** Where the menu's search leads: `/cards/<id>` or `/board?q=…` (`/board` for nothing typed). */
export async function searchTarget(text: string): Promise<string> {
  const q = text.trim();
  if (!q) return "/board";
  const board = `/board?q=${encodeURIComponent(q)}`;
  const number = cardNumber(q);
  if (!number) return board;
  try {
    // the search puts the card of that number first, then the cards that mention it
    const found = await apiGet<CardCompact[]>(`/api/cards?q=${encodeURIComponent(q)}`);
    const hits = found.filter((c) => (number.key ? c.id.toUpperCase() === number.key : c.n === number.n));
    return hits.length === 1 ? `/cards/${encodeURIComponent(hits[0].id)}` : board;
  } catch {
    return board; // the board searches again and says what went wrong
  }
}
