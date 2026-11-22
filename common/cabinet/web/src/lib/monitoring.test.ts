import { beforeEach, describe, expect, it } from "vitest";
import { setLocale } from "./i18n.svelte";
import { activeCards, activeIncidents, cardFieldPattern, changeText, checkTitle, explanation, incidentActions, incidentAsk, needsAttention, notificationText, quietState } from "./monitoring";
import { describeEvent } from "./events";
import type { Incident, LinkedCard, ServerCheck, TrustDecision } from "./types";

beforeEach(() => setLocale("ru"));
describe("monitoring decisions", () => {
  it("retains acknowledged and muted incidents while hiding terminal work", () => {
    const items = ["open", "acknowledged", "resolved"].map(status => ({status}) as Incident);
    expect(activeIncidents(items)).toHaveLength(2);
    const cards = ["Inbox", "Blocked", "Review", "Done", "Cancelled"].map(status => ({status}) as LinkedCard);
    expect(activeCards(cards).map(c => c.status)).toEqual(["Inbox", "Blocked", "Review"]);
  });
  it("keeps failures and overdue observations visible even when their last measurement was ok", () => {
    const row = {check:"disk", level:"ok", freshness:"fresh"} as ServerCheck;
    expect(needsAttention(row)).toBe(false);
    expect(needsAttention({...row, freshness:"stale"})).toBe(true);
    expect(needsAttention({...row, freshness:"error"})).toBe(true);
    expect(needsAttention({...row, level:"crit", freshness:"disabled"})).toBe(false);
  });
  it("explains the service mask and translates collection failures", () => {
    expect(checkTitle("sec:units", "ModemManager.service")).toContain("служб systemd");
    expect(checkTitle("gate:ports")).toContain("Сбор данных");
    expect(explanation({check:"sec:units", subject:"x", value:"symlink -> /dev/null", text:""})).toContain("блокирует запуск");
  });
  it("keeps acknowledgement and timed silence independent", () => {
    const incident = {status:"acknowledged", muted_until:"2026-10-24T12:00:00Z"} as Incident;
    const before = notificationText(incident, Date.parse("2026-10-23T12:00:00Z"));
    expect(before).toContain("Ознакомлен");
    expect(before).toContain("приостановлены");
    expect(notificationText(incident, Date.parse("2026-10-25T12:00:00Z"))).toBe("Ознакомлен");
  });
  it("never exposes missing event placeholders", () => {
    const event = {seq:1, ts:"", actor:"system:monitor", kind:"incident.opened", card:"", payload:{level:"warn", value:"old"}};
    const text = describeEvent(event);
    expect(text).not.toContain("undefined");
    expect(text).not.toContain("{server}");
    expect(text).toContain("old");
  });
});

describe("cardFieldPattern", () => {
  // the pattern attribute of the card field: the host's card prefix in any case, then the number
  const accepts = (prefix: string, value: string) => new RegExp(`^(?:${cardFieldPattern(prefix)})$`).test(value);

  it("takes a card of the host's prefix in either case", () => {
    expect(accepts("KD", "KD-123")).toBe(true);
    expect(accepts("KD", "kd-7")).toBe(true);
    expect(accepts("AC", "ac-7")).toBe(true);
  });

  it("refuses another prefix and a bare number", () => {
    expect(accepts("KD", "AC-7")).toBe(false);
    expect(accepts("AC", "KD-7")).toBe(false);
    expect(accepts("KD", "123")).toBe(false);
  });
});

describe("incidentAsk (KD-9189)", () => {
  // what the incident's page asks of the owner follows its check and its state (L1, L2)
  const base = {status:"open", check:"disk", subject:"/", investigation:{state:"waiting", detail:"", report:null}} as unknown as Incident;
  const page = (over: Partial<Incident> = {}, cards: LinkedCard[] = [], acceptable = false) =>
    ({incident:{...base, ...over} as Incident, cards, acceptable});
  const card = (status: string) => ({id:"KD-7", title:"x", status, project:"admin", assignee:"@sysadmin"}) as LinkedCard;
  const report = {body:"b", summary:"s", author:"agent:sysadmin", created:"2026-10-26T12:00:00Z", thread:1, seq:1};

  it("asks nothing of a closed incident", () => {
    expect(incidentAsk(page({status:"resolved"}, [card("In Progress")], true))).toEqual({step:"closed"});
  });
  it("follows the live card, but not a closed one", () => {
    expect(incidentAsk(page({}, [card("Done"), card("In Progress")]))).toEqual({step:"card", card:card("In Progress")});
    expect(incidentAsk(page({investigation:{state:"running", detail:"", report:null}}, [card("Done")])).step).toBe("investigating");
  });
  it("counts a wake due, in flight or a finished session without its report as an investigation", () => {
    // «waiting»: the monitor wakes the sysadmin on every opening it was not told to drop
    for (const state of ["waiting", "queued", "running", "awaiting_report"] as const)
      expect(incidentAsk(page({investigation:{state, detail:"", report:null}})).step).toBe("investigating");
  });
  it("asks whether a difference from the baseline is the owner's own change", () => {
    expect(incidentAsk(page({check:"sec:cron", investigation:{state:"reviewed", detail:"", report}}, [], true)))
      .toEqual({step:"confirm_change", login:false});
    expect(incidentAsk(page({check:"logins", subject:"u@key SHA256:x"}, [], true))).toEqual({step:"confirm_change", login:true});
  });
  it("has the owner read a ready investigation of a threshold", () => {
    expect(incidentAsk(page({investigation:{state:"reviewed", detail:"", report}})).step).toBe("review_report");
  });
  // the owner's signed «accept» waits for the monitor's next tick: the page says so and does not ask again
  const decision = (state: string, apply_state: string, created: string, apply_detail = "") =>
    ({id:created, type:"monitor_trust", state, apply_state, apply_detail, created, decided_at:created,
      payload:{kind:"accept", incident:483}}) as unknown as TrustDecision;
  const signed = (trust: TrustDecision[], cards: LinkedCard[] = []) =>
    incidentAsk({...page({check:"sec:cron"}, cards, true), trust});

  it("says a signed acceptance waits for the monitor, even over a card", () => {
    expect(signed([decision("approved", "pending", "2026-11-01T08:52:01Z")], [card("In Progress")]))
      .toEqual({step:"accepting", at:"2026-11-01T08:52:01Z"});
  });
  it("asks again, with the reason, when the monitor could not apply the last acceptance", () => {
    expect(signed([decision("approved", "error", "2026-11-01T08:52:01Z", "baseline is busy")]))
      .toEqual({step:"confirm_change", login:false, failed:"baseline is busy"});
    // a later signature wins over an older failure; an unsigned proposal changes nothing
    expect(signed([decision("approved", "error", "2026-11-01T08:50:00Z", "x"), decision("approved", "pending", "2026-11-01T08:52:00Z")]).step)
      .toBe("accepting");
    expect(signed([decision("open", "", "2026-11-01T08:53:00Z")])).toEqual({step:"confirm_change", login:false});
  });
  // a signed "access revoked" waits the same way, and its failure says it was not a baseline accept
  const revoke = (apply_state: string, apply_detail = "") =>
    ({...decision("approved", apply_state, "2026-11-11T09:00:00Z", apply_detail), payload:{kind:"revoke", incident:603}}) as TrustDecision;
  it("says a signed «доступ снят» waits for the monitor, and asks again when the key was still there", () => {
    expect(signed([revoke("pending")])).toEqual({step:"accepting", at:"2026-11-11T09:00:00Z", revoke:true});
    expect(signed([revoke("error", "ключ ещё разрешён на arm")]))
      .toEqual({step:"confirm_change", login:false, failed:"ключ ещё разрешён на arm", revokeFailed:true});
  });
  it("waits for recovery when nobody investigates", () => {
    expect(incidentAsk(page({investigation:{state:"failed", detail:"", report:null}})).step).toBe("wait_recovery");
    expect(incidentAsk(page({investigation:undefined})).step).toBe("wait_recovery");
  });
});

describe("incidentActions (KD-9189, V2)", () => {
  // the "Actions" menu: what the question's own buttons do not already offer
  const card = {step:"card", card:{id:"KD-7", title:"x", status:"In Progress", project:"admin", assignee:"@sysadmin"}} as const;
  const all = {canTrust:true, canAct:true, quiet:false};

  it("offers nothing on a closed incident", () => {
    expect(incidentActions({step:"closed"}, all)).toEqual([]);
  });
  it("leaves accepting to the question when the question asks it, and offers no second one while it applies", () => {
    expect(incidentActions({step:"confirm_change", login:false}, all)).toEqual(["quiet", "recheck", "link"]);
    expect(incidentActions({step:"accepting", at:"2026-11-01T08:52:01Z"}, all)).toEqual(["quiet", "recheck", "link"]);
  });
  it("puts accepting first when a card leads and the person may sign", () => {
    expect(incidentActions(card, all)).toEqual(["accept", "quiet", "recheck", "link"]);
    expect(incidentActions(card, {...all, canTrust:false})).toEqual(["quiet", "recheck", "link"]);
  });
  it("offers «доступ снят» beside accepting, never over the question's own buttons or while it applies", () => {
    const may = {...all, canRevoke:true};
    expect(incidentActions(card, may)).toEqual(["accept", "revoke", "quiet", "recheck", "link"]);
    expect(incidentActions({step:"confirm_change", login:true}, may)).toEqual(["quiet", "recheck", "link"]);
    expect(incidentActions({step:"accepting", at:"x", revoke:true}, may)).toEqual(["quiet", "recheck", "link"]);
  });
  it("keeps reminders and the recheck to who may act on incidents", () => {
    expect(incidentActions({step:"wait_recovery"}, {...all, canTrust:false, canAct:false})).toEqual(["link"]);
  });
  it("turns reminders back on when they are off", () => {
    expect(incidentActions({step:"investigating"}, {...all, canTrust:false, quiet:true})).toEqual(["unquiet", "recheck", "link"]);
  });
});

describe("changeText and quietState (KD-9189)", () => {
  it("says when the content changed and how often since the opening, without hashes", () => {
    const i = {opened_at:"2026-10-26T11:35:01Z", value:"8118fa84273656b1a165580c7d4a7fdca369fdae61805ba31ce7eeaf4db29f2b"} as Incident;
    const once = changeText(i, {count:0, last_at:""});
    expect(once).toContain("впервые");
    expect(once).not.toContain("после открытия");
    const again = changeText(i, {count:2, last_at:"2026-10-30T09:00:00Z"});
    expect(again).toContain("после открытия");
    expect(again).toContain("2");
    expect(again).not.toContain("8118");
  });
  it("is one state: until closed wins over a timed mute", () => {
    const now = Date.parse("2026-10-23T12:00:00Z");
    expect(quietState({status:"open", muted_until:""} as Incident, now)).toBeNull();
    expect(quietState({status:"open", muted_until:"2026-10-23T11:00:00Z"} as Incident, now)).toBeNull();
    expect(quietState({status:"open", muted_until:"2026-10-24T12:00:00Z"} as Incident, now)).toEqual({until:"2026-10-24T12:00:00Z"});
    expect(quietState({status:"acknowledged", muted_until:"2026-10-24T12:00:00Z"} as Incident, now)).toEqual({until:"closed"});
  });
});
