// the version a Claude model's name carries, as people say it.
import { expect, it } from "vitest";
import { claudeVersion } from "./modelversion";

it.each([
  ["claude-opus-5-5", "5.5"],
  ["claude-fable-5-2", "5.2"],
  ["claude-opus-4-5-20251101", "4.5"],
  ["claude-sonnet-4-20250514", "4"],
  ["claude-3-5-sonnet-20241022", "3.5"],
  ["claude-3-opus-20240229", "3"],
  ["claude-3-5-sonnet-latest", "3.5"],
])("%s reads as %s", (name, version) => {
  expect(claudeVersion(name)).toBe(version);
});

// a name of another shape is shown as it is: a number is never guessed
it.each(["claude-opus-next", "claude-opus-5-5-1", "claude-opus-2026", "or-gpt5", "gpt-5", "claude", ""])("%s names no version", (name) => {
  expect(claudeVersion(name)).toBe("");
});
