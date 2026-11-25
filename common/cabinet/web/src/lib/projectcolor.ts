// A colour per project — the board's filter chip and the card's project chip, so a card says whose it is at a
// glance. The hues are app.css's --project-0…; none of them is the green, amber or red of a status.
export const PROJECT_HUES = 6;

function hash(text: string): number {
  let h = 2166136261; // FNV-1a
  for (let i = 0; i < text.length; i++) h = Math.imul(h ^ text.charCodeAt(i), 16777619);
  return h >>> 0;
}

/** Each project's hue (0 … PROJECT_HUES-1): its id's own while free, else the next free one, the projects taken in
 * the order of their ids — the same for any order of the list, and a project with a later id leaves the others' be. */
export function projectHues(ids: string[]): Map<string, number> {
  const hues = new Map<string, number>();
  const taken = new Set<number>();
  for (const id of [...new Set(ids)].sort()) {
    let hue = hash(id) % PROJECT_HUES;
    if (taken.size < PROJECT_HUES) while (taken.has(hue)) hue = (hue + 1) % PROJECT_HUES;
    taken.add(hue);
    hues.set(id, hue);
  }
  return hues;
}
