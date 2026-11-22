import { cleanup, render } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import type { Incident } from "../lib/types";
import IncidentRows from "./IncidentRows.svelte";

function incident(n: number, server: string, over: Partial<Incident> = {}): Incident {
  return {
    n, id: `P-${n}`, key: `${server}:units:logrotate.service`, server, check: "units", subject: "logrotate.service",
    level: "warn", max_level: "warn", value: "failed", text: "", status: "open", muted_until: "",
    opened_at: "2026-11-13T21:00:01Z", last_seen: "2026-11-14T14:25:01Z", resolved_at: "", fixed_card: "",
    repeats: 0, ...over,
  };
}

const top = (root: HTMLElement) => [...root.querySelectorAll(".incident-card-top")];
const chips = (el: Element) => [...el.querySelectorAll(".incident-server")].map((c) => c.textContent?.trim());

beforeEach(() => setLocale("ru"));
afterEach(cleanup);

describe("IncidentRows", () => {
  it("puts each incident's server as a chip beside its number, not among the details below", () => {
    const { container } = render(IncidentRows, {
      items: [incident(692, "example-prod"), incident(693, "example-stage")], showServer: true,
    });
    expect(top(container).map(chips)).toEqual([["example-prod"], ["example-stage"]]);
    const meta = [...container.querySelectorAll(".monitor-meta")].map((m) => m.textContent ?? "");
    expect(meta.some((m) => m.includes("example-"))).toBe(false);
  });

  it("a group names the servers of its members as chips in its own row", () => {
    const group = { id: "g1", size: 2, servers: 2, subjects: 1 };
    const { container } = render(IncidentRows, {
      items: [incident(692, "example-prod", { group }), incident(693, "example-stage", { group })], showServer: true,
    });
    const summary = container.querySelector(".incident-group summary .incident-card-top");
    expect(summary && chips(summary)).toEqual(["example-prod", "example-stage"]);
  });

  it("a group with more servers than the page loaded counts the rest", () => {
    const group = { id: "g1", size: 5, servers: 5, subjects: 1 };
    const { container } = render(IncidentRows, {
      items: [incident(692, "example-prod", { group }), incident(693, "example-stage", { group })], showServer: true,
    });
    const summary = container.querySelector(".incident-group summary .incident-card-top");
    expect(summary?.textContent).toContain("+3");
  });

  it("one server's page shows no server chip", () => {
    const { container } = render(IncidentRows, { items: [incident(692, "example-prod")] });
    expect(container.querySelectorAll(".incident-server")).toHaveLength(0);
  });
});
