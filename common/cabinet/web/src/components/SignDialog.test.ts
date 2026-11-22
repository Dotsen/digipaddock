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
  // a server plan and a critical command take a passkey only (the target checks that signature) — the
  // page offers no code for them; a factory plan and the other types keep the code as before
  it("offers no TOTP code for a server plan or a critical command", () => {
    render(SignDialog, { items: [item("admin_plan", { ttl_options: [24, 72, 168] })], onclose: noop, ondone: noop, onreread: noop });
    expect(screen.queryByText("Ввести код TOTP")).toBeNull();
    expect(screen.getByText("Только passkey: подпись проверяет сервер, код он проверить не может")).toBeTruthy();
    cleanup();
    render(SignDialog, { items: [item("admin_critical")], onclose: noop, ondone: noop, onreread: noop });
    expect(screen.queryByText("Ввести код TOTP")).toBeNull();
  });

  // the policy of every decision in a batch counts, not only of the first
  it("offers no TOTP code when any plan of a batch is a server plan", () => {
    const factory = item("admin_plan", { policy: "factory", ttl_options: [24] });
    const server = item("admin_plan", { ttl_options: [24] });
    server.decision = { ...server.decision, id: "fedcba9876543210fedcba9876543210" };
    render(SignDialog, { items: [factory, server], onclose: noop, ondone: noop, onreread: noop });
    expect(screen.queryByText("Ввести код TOTP")).toBeNull();
    expect(screen.getByText("Только passkey: подпись проверяет сервер, код он проверить не может")).toBeTruthy();
  });

  it("keeps the code for a factory plan and a promote", () => {
    render(SignDialog, { items: [item("admin_plan", { policy: "factory", ttl_options: [24] })], onclose: noop, ondone: noop, onreread: noop });
    expect(screen.getByText("Ввести код TOTP")).toBeTruthy();
    cleanup();
    render(SignDialog, { items: [item("promote")], onclose: noop, ondone: noop, onreread: noop });
    expect(screen.getByText("Ввести код TOTP")).toBeTruthy();
  });
});
