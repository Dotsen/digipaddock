import { describe, expect, it } from "vitest";
import { ID_PATTERN, slug } from "./slug";

describe("slug", () => {
  // the same names as the server's test (common/tests/test_cabinet_projects.py)
  it.each([
    ["DIGIPADDOCK", "digipaddock"],
    ["Сайт и лендинги", "sayt-i-lendingi"],
    ["Example Servers of the platform 2", "example-servers-of-the-platform"],
    ["2026 бюджет", "byudzhet"],
    ["Alert.Examples.Co", "alert-examples-co"],
    ["Объём «Ёжик»", "obem-ezhik"],
    ["123", ""],
  ])("%s -> %s", (name, want) => {
    expect(slug(name)).toBe(want);
    if (want) expect(new RegExp(`^${ID_PATTERN}$`).test(want)).toBe(true);
  });
});
