// the servers section's addresses, order and refreshes.
import { describe, expect, it } from "vitest";
import { MUTES, checkName, checking, incidentHref, serverHref, shortValue, sortServers, touchesServers } from "./servers";
import type { CheckRequest, ServerRow } from "./types";

function server(alias: string, over: Partial<ServerRow> = {}): ServerRow {
  return {
    alias, host: "", enabled: 1, reachable: 1, unreachable_count: 0, last_ok: "", updated: "", worst: "ok",
    checks: { ok: 1, warn: 0, crit: 0 }, incidents: 0, check_now: null, ...over,
  };
}

describe("the servers section", () => {
  it("links incidents and servers into the cabinet", () => {
    expect(incidentHref("P-35")).toBe("/incidents/P-35");
    expect(serverHref("node-f")).toBe("/servers/node-f");
    expect(checkName("disk", "/")).toBe("disk /");
    expect(checkName("reach", "-")).toBe("reach");
    expect(checkName("reach", "")).toBe("reach");
  });

  it("cuts a digest in a summary line and leaves any other value", () => {
    expect(shortValue("3f9a1c0d2b7e" + "a".repeat(52))).toBe("3f9a1c0d2b7e…");
    expect(shortValue("deadbeef")).toBe("deadbeef");
    expect(shortValue("91%")).toBe("91%");
    expect(shortValue(null)).toBe("");
  });

  it("puts the worst servers first and the switched-off last", () => {
    const rows = [
      server("ok-b"), server("off", { enabled: 0, worst: "crit" }), server("warn", { worst: "warn" }),
      server("down", { reachable: 0 }), server("crit", { worst: "crit" }), server("ok-a"), server("new", { worst: "" }),
    ];
    expect(sortServers(rows).map((r) => r.alias)).toEqual(["down", "crit", "warn", "ok-a", "ok-b", "new", "off"]);
  });

  it("mutes for an hour, a day or a week", () => {
    expect(MUTES).toEqual(["1h", "1d", "7d"]);
  });

  it("keeps «Проверить сейчас» off while the request waits for the monitor", () => {
    const request = (state: CheckRequest["state"]): CheckRequest => ({
      id: "r", server: "node-c", state, requested_by: "user:owner", detail: "", created: "", started: "", finished: "",
    });
    expect(checking(null)).toBe(false);
    expect(checking(request("queued"))).toBe(true);
    expect(checking(request("running"))).toBe(true);
    expect(checking(request("done"))).toBe(false);
    expect(checking(request("failed"))).toBe(false);
  });

  it("refreshes on the incidents' and the checks' events only", () => {
    expect(touchesServers({ kind: "incident.acknowledged" })).toBe(true);
    expect(touchesServers({ kind: "server.check" })).toBe(true);
    expect(touchesServers({ kind: "card.changed" })).toBe(false);
  });
});
