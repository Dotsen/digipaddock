import { describe, expect, it } from "vitest";
import { agentUnread, eventSpace, parseSpace, spaceBase, spaceKey } from "./spaces";

describe("an agent's threads (KD-28)", () => {
  it("keys the chat by the agent and «Монитор» as <agent>/monitor, as the server's unread counts", () => {
    expect(spaceKey("sysadmin")).toBe("sysadmin");
    expect(spaceKey("sysadmin", "monitor")).toBe("sysadmin/monitor");
    expect(parseSpace("sysadmin/monitor")).toEqual({ agent: "sysadmin", space: "monitor" });
    expect(parseSpace("boss")).toEqual({ agent: "boss", space: "chat" });
  });

  it("addresses the API of each thread", () => {
    expect(spaceBase("sysadmin")).toBe("/api/agents/sysadmin");
    expect(spaceBase("sysadmin/monitor")).toBe("/api/agents/sysadmin/monitor");
  });

  it("tells which thread an event is about", () => {
    expect(eventSpace({ agent: "sysadmin", space: "monitor" })).toBe("sysadmin/monitor");
    expect(eventSpace({ agent: "sysadmin" })).toBe("sysadmin");
    expect(eventSpace({})).toBeNull();
    expect(eventSpace(undefined)).toBeNull();
  });

  it("sums an agent's unread replies over its threads", () => {
    const all = { sysadmin: { unread: 1 }, "sysadmin/monitor": { unread: 3 }, boss: { unread: 5 } };
    expect(agentUnread(all, "sysadmin")).toBe(4);
    expect(agentUnread(all, "dev")).toBe(0);
  });
});
