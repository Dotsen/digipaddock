// An overview answer for the tests: everything empty unless given.
import type { Overview } from "../types";

export function answer(over: Partial<Overview> = {}): Overview {
  return {
    needs: [], work: [], problems: [], changes: [], team: [], servers: null,
    counts: { needs: 0, work: 0, problems: 0, changes_new: 0 }, seen: 0, watermark: 1, ...over,
  };
}
