// A colour per project: the filter chip's border and the card's project chip, the same everywhere.
import { describe, expect, it } from "vitest";
import { PROJECT_HUES, projectHues } from "./projectcolor";

describe("projectHues", () => {
  it("gives projects different hues while there are hues enough", () => {
    const ids = ["alert", "api", "arm", "digipaddock", "keeta", "shop"];
    const hues = projectHues(ids);
    expect(new Set(ids.map((id) => hues.get(id))).size).toBe(PROJECT_HUES);
    for (const id of ids) expect(hues.get(id)).toBeGreaterThanOrEqual(0);
    for (const id of ids) expect(hues.get(id)).toBeLessThan(PROJECT_HUES);
  });

  it("keeps a project's hue whatever the order of the list, and when a project of a later id comes", () => {
    const before = projectHues(["alert", "api", "arm"]);
    expect(projectHues(["arm", "alert", "api"])).toEqual(before);
    const after = projectHues(["alert", "api", "arm", "zeta"]);
    for (const id of ["alert", "api", "arm"]) expect(after.get(id)).toBe(before.get(id));
  });

  it("repeats hues past their number, never leaving a project without one", () => {
    const ids = Array.from({ length: PROJECT_HUES + 3 }, (_, i) => `p${i}`);
    const hues = projectHues(ids);
    expect(ids.every((id) => (hues.get(id) ?? -1) >= 0)).toBe(true);
  });
});
