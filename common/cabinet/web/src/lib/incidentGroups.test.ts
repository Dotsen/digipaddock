import { describe, expect, it } from "vitest";
import { ApiError } from "./api";
import { acceptable, groupRows, revocable, runMany, toAccept } from "./incidentGroups";
import { bundleRows, rowCount, type InboxRow } from "./stores/inbox.svelte";
import type { Incident, Level } from "./types";

const AT = "2026-11-03T10:00:00Z";

function incident(n: number, over: Partial<Incident> = {}): Incident {
  return {
    n, id: `P-${n}`, key: `node-${n}:sec:units:u${n}`, server: `node-${n}`, check: "sec:units", subject: `u${n}`,
    level: "warn", max_level: "warn", value: "", text: "", status: "open", muted_until: "", opened_at: AT,
    last_seen: AT, resolved_at: "", fixed_card: "", repeats: 0, ...over,
  };
}

const GROUP = { id: `${AT}|sec:units`, size: 26, servers: 13, subjects: 2 };

describe("incident groups (KD-9196)", () => {
  it("folds the members of one group into one row, in the order of first appearance, at its worst level", () => {
    const lone = incident(100, { opened_at: "2026-11-03T09:00:00Z", check: "disk", subject: "/" });
    const members = Array.from({ length: 26 }, (_, k) =>
      incident(k + 1, { group: GROUP, level: (k === 7 ? "crit" : "warn") as Level }));
    const rows = groupRows([...members.slice(0, 3), lone, ...members.slice(3), incident(101, { check: "ports" })]);
    expect(rows.map((r) => r.kind)).toEqual(["group", "one", "one"]);
    const group = rows[0];
    if (group.kind !== "group") throw new Error("not a group");
    expect(group).toMatchObject({ id: GROUP.id, size: 26, servers: 13, subjects: 2, check: "sec:units", level: "crit" });
    expect(group.members).toHaveLength(26);
    expect(rows[1]).toEqual({ kind: "one", incident: lone });
  });

  it("knows what may close as «доступ снят»: a login by a key only (KD-9272)", () => {
    const login = (subject: string, over: Partial<Incident> = {}) => incident(1, { check: "logins", subject, ...over });
    expect(revocable(login("promo@ED25519 SHA256:x"))).toBe(true);
    for (const subject of ["gap", "root@password", "root@key ?"]) expect(revocable(login(subject))).toBe(false);
    expect(revocable(incident(2))).toBe(false);
    expect(revocable(login("promo@ED25519 SHA256:x", { status: "resolved" }))).toBe(false);
    const signed = { decision: "d", apply_state: "pending" as const, decided_at: AT, kind: "revoke" as const };
    expect(revocable(login("promo@ED25519 SHA256:x", { accepted: signed }))).toBe(false);
    expect(toAccept([login("a@ED25519 SHA256:a"), incident(3)], revocable).ids).toEqual(["P-1"]);
  });

  it("knows what the owner may accept into the baseline", () => {
    expect(acceptable(incident(1))).toBe(true);
    expect(acceptable(incident(2, { check: "ports" }))).toBe(true);
    expect(acceptable(incident(3, { check: "logins", subject: "root@key" }))).toBe(true);
    expect(acceptable(incident(4, { check: "logins", subject: "gap" }))).toBe(false);
    expect(acceptable(incident(5, { check: "mem" }))).toBe(false);
    expect(acceptable(incident(6, { status: "resolved" }))).toBe(false);
    // signed into the baseline, the monitor's tick has not closed it yet
    expect(acceptable(incident(7, { accepted: { decision: "d", apply_state: "pending", decided_at: "" } }))).toBe(false);
  });

  it("runs an action incident by incident and reports each one", async () => {
    const calls: string[] = [];
    const post = async (path: string) => {
      calls.push(path);
      if (path.includes("P-2")) throw new ApiError(409, "conflict", "changed");
      return {};
    };
    const got = await runMany(["P-1", "P-2", "P-3"], "mute", { for: "1d" }, post);
    expect(calls).toEqual(["/api/incidents/P-1/mute", "/api/incidents/P-2/mute", "/api/incidents/P-3/mute"]);
    expect(got.map((r) => [r.id, r.ok])).toEqual([["P-1", true], ["P-2", false], ["P-3", true]]);
    expect(got[1].error).toBeInstanceOf(ApiError);
  });

  it("shows the decisions of one bundle as one inbox row", () => {
    const row = (id: string, bundle = "", server = "node-a"): InboxRow => ({
      decision: { id, type: "monitor_trust", payload: { kind: "accept", server, ...(bundle ? { bundle } : {}) } } as any,
      view: {} as any,
    });
    const rows = bundleRows([row("d1", "b1"), row("d2"), row("d3", "b1", "node-b"), row("d4", "b1")]);
    expect(rows.map((r) => r.decision.id)).toEqual(["d1", "d2"]);
    expect(rows[0].members?.map((m) => m.decision.id)).toEqual(["d1", "d3", "d4"]);
    expect(rows[1].members).toBeUndefined();
    expect(rowCount([row("d1", "b1"), row("d2"), row("d3", "b1", "node-b")])).toBe(2);
  });

  it("takes at most 50 acceptable incidents for one signature and says how many wait (review 3)", () => {
    const many = Array.from({ length: 53 }, (_, k) => incident(k + 1));
    expect(toAccept([incident(1, { check: "mem" }), incident(2)])).toEqual({ ids: ["P-2"], over: 0 });
    const got = toAccept(many);
    expect(got.ids).toHaveLength(50);
    expect(got.ids[0]).toBe("P-1");
    expect(got.over).toBe(3);
  });
});
