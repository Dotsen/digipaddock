// the version a Claude model's name carries, as people say it — «5.5» of `claude-opus-5-5`.

/** The version of a Claude model by its id: what is left after `claude`, the family's words and a date is one or
 * two short numbers — `claude-opus-4-5-20251101` → `4.5`, `claude-3-5-sonnet-20241022` → `3.5`. `""` for a name of
 * another shape: it is shown as it is, a number is never guessed. */
export function claudeVersion(name: string): string {
  const parts = name.split("-");
  if (parts[0] !== "claude") return "";
  const numbers = parts.slice(1).filter((part) => !/^[a-z]+$/.test(part) && !/^\d{8}$/.test(part));
  const short = numbers.length >= 1 && numbers.length <= 2 && numbers.every((part) => /^\d{1,2}$/.test(part));
  return short ? numbers.join(".") : "";
}
