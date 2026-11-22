<script module lang="ts">
  import type { Key } from "../lib/i18n.svelte";

  const KEYS: Record<string, Key> = {
    Inbox: "status.inbox",
    "To Do": "status.todo",
    "In Progress": "status.in_progress",
    Review: "status.review",
    Blocked: "status.blocked",
    Done: "status.done",
    Cancelled: "status.cancelled",
  };
  const TONES: Record<string, string> = {
    Inbox: "",
    "To Do": "info",
    "In Progress": "acc",
    Review: "warn",
    Blocked: "crit",
    Done: "good",
    Cancelled: "",
  };

  export function statusKey(status: string): Key {
    return KEYS[status] ?? "status.inbox";
  }

  export function statusTone(status: string): string {
    return TONES[status] ?? "";
  }
</script>

<script lang="ts">
  // A card's status as a chip (the card page changes it through ChipMenu).
  import { t } from "../lib/i18n.svelte";

  let { status }: { status: string } = $props();
</script>

<span class="chip status {statusTone(status)}"><span class="dot"></span>{t(statusKey(status))}</span>

<style>
  .status { color: var(--text); }
</style>
