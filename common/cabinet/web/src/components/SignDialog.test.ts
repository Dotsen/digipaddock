import { cleanup, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import type { Decision, DecisionItem, DecisionView } from "../lib/types";
import SignDialog from "./SignDialog.svelte";

function item(type: string, view: DecisionView = {}): DecisionItem {
  const decision: Decision = {
    id: "0123456789abcdef0123456789abcdef",
    type,
    card: "KD-2",
    project: "admin",
    state: "open",
    step_up: true,
    decide_permission: `decide.${type}`,
    payload: {},
    created: "2026-10-15T11:00:00Z",
    expires: "2099-01-01T00:00:00Z",
    requested_by: "agent:sysadmin",
    decided_by: "",
    decided_at: "",
    signature: null,
    reply: "",
    question: "",
  };
  return { decision, view };
}

const noop = () => {};

beforeEach(() => setLocale("ru"));
afterEach(cleanup);

describe("SignDialog", () => {
  // a plan (a server's or the factory's), a critical command and a monitor trust take a passkey only — the
  // page offers no code for them; the other types keep the code
  it("offers no TOTP code for a plan, a critical command or a monitor trust", () => {
    render(SignDialog, { items: [item("admin_plan", { ttl_options: [24, 72, 168] })], onclose: noop, ondone: noop, onreread: noop });
    expect(screen.queryByText("Ввести код TOTP")).toBeNull();
    expect(screen.getByText("Только passkey: код TOTP эту подпись не заменит")).toBeTruthy();
    cleanup();
    render(SignDialog, { items: [item("admin_critical")], onclose: noop, ondone: noop, onreread: noop });
    expect(screen.queryByText("Ввести код TOTP")).toBeNull();
    cleanup();
    render(SignDialog, { items: [item("admin_plan", { policy: "factory", ttl_options: [24] })], onclose: noop, ondone: noop, onreread: noop });
    expect(screen.queryByText("Ввести код TOTP")).toBeNull();
    cleanup();
    render(SignDialog, { items: [item("monitor_trust")], onclose: noop, ondone: noop, onreread: noop });
    expect(screen.queryByText("Ввести код TOTP")).toBeNull();
  });

  // the policy of every decision in a batch counts, not only of the first
  it("offers no TOTP code when any plan of a batch is a server plan", () => {
    const factory = item("admin_plan", { policy: "factory", ttl_options: [24] });
    const server = item("admin_plan", { ttl_options: [24] });
    server.decision = { ...server.decision, id: "fedcba9876543210fedcba9876543210" };
    render(SignDialog, { items: [factory, server], onclose: noop, ondone: noop, onreread: noop });
    expect(screen.queryByText("Ввести код TOTP")).toBeNull();
    expect(screen.getByText("Только passkey: код TOTP эту подпись не заменит")).toBeTruthy();
  });

  it("keeps the code for a promote", () => {
    render(SignDialog, { items: [item("promote")], onclose: noop, ondone: noop, onreread: noop });
    expect(screen.getByText("Ввести код TOTP")).toBeTruthy();
  });
});
