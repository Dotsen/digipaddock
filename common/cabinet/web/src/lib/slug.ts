// A project's id from its name, as the server makes it (kd.cabinet.projects_api.slug): lower case,
// Cyrillic in Latin letters, anything else a dash, a letter first, at most 32. The form shows it before saving.
export const ID_MAX = 32;
export const ID_PATTERN = "[a-z][a-z0-9\\-]{0,31}";

const LATIN: string[] = [
  "a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u", "f",
  "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya",
];
const TRANSLIT = new Map([..."абвгдеёжзийклмнопрстуфхцчшщъыьэюя"].map((c, i) => [c, LATIN[i]]));

export function slug(name: string): string {
  const text = [...name.toLowerCase()].map((c) => TRANSLIT.get(c) ?? c).join("");
  return text
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^[^a-z]+/, "")
    .slice(0, ID_MAX)
    .replace(/^-+|-+$/g, "");
}
