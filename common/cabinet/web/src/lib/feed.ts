// The feed's kinds filter: a few groups of journal kinds the server narrows by (`kinds=a,b`).
export const FEED_GROUPS = {
  cards: ["card.created", "card.changed", "card.imported"],
  messages: ["message.added", "message.changed"],
  decisions: [
    "decision.opened",
    "decision.approved",
    "decision.rejected",
    "decision.asked",
    "decision.replied",
    "decision.expired",
    "decision.revoked",
  ],
  digest: ["digest"],
  incidents: [
    "incident.opened",
    "incident.worse",
    "incident.better",
    "incident.fixed",
    "incident.fix_failed",
    "incident.resolved",
    "incident.acknowledged",
    "incident.muted",
    "incident.unmuted",
    "incident.changed", // a difference from the baseline changed again after the opening
    "server.check", // "Check now"
    "monitor.settings", // the monitor's settings saved in the cabinet
  ],
  account: [
    "llm_key.changed",
    "user.created",
    "user.updated",
    "user.role_set",
    "user.prefs",
    "user.passkey_added",
    "user.passkey_removed",
    "user.passkey_signing",
    "user.session_opened",
    "user.session_closed",
    "user.code_issued",
    "push.subscribed",
    "push.unsubscribed",
  ],
} as const;

export type FeedGroup = keyof typeof FEED_GROUPS;

export function feedQuery(params: { project?: string; group?: string; before?: number; after?: number; limit?: number }): string {
  const query = new URLSearchParams();
  if (params.limit) query.set("limit", String(params.limit));
  if (params.project) query.set("project", params.project);
  if (params.group && params.group in FEED_GROUPS) query.set("kinds", FEED_GROUPS[params.group as FeedGroup].join(","));
  if (params.before !== undefined) query.set("before", String(params.before));
  if (params.after !== undefined) query.set("after", String(params.after));
  return `/api/feed?${query}`;
}
