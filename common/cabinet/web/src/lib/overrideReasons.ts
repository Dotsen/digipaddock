// The ready reasons of a manual status change by its target status, «other» last. The service holds the
// same list (kd/cabinet/api.py OVERRIDE_REASONS, a pytest keeps them equal); labels are flow.override.code.<code>.
export const OVERRIDE_OTHER = "other";
export const OVERRIDE_DUPLICATE = "duplicate";

export const OVERRIDE_REASONS: Record<string, readonly string[]> = {
  "Inbox": ["rewrite_brief", "started_by_mistake", "other"],
  "To Do": ["restart", "requeue", "other"],
  "In Progress": ["continue", "rework", "other"],
  "Review": ["ready_for_review", "other"],
  "Blocked": ["waits_card", "waits_external", "other"],
  "Done": ["done_outside", "other"],
  "Cancelled": ["not_relevant", "duplicate", "other"],
};

/** What the dialog sends with a manual status change. */
export type OverrideReason = { code: string; text: string; canonical?: string };

export function overrideReasons(status: string): readonly string[] {
  return OVERRIDE_REASONS[status] ?? [OVERRIDE_OTHER];
}
