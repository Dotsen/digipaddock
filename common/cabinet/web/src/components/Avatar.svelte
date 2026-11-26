<script module lang="ts">
  // Initials on the author's tint: agent, owner (a person), boss, monitor/system.
  export type AuthorKind = "agent" | "owner" | "boss" | "monitor";
</script>

<script lang="ts">
  let { name, kind = "agent", size = 30 }: { name: string; kind?: AuthorKind; size?: number } = $props();

  const initials = $derived(
    name
      .replace(/^@/, "")
      .split(/[\s._-]+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((part) => part[0]!.toUpperCase())
      .join("") || "?",
  );
</script>

<span
  class="avatar {kind}"
  style:width="{size}px"
  style:height="{size}px"
  style:font-size="{Math.round(size * 0.4)}px"
  aria-hidden="true">{initials}</span
>

<style>
  .avatar {
    border-radius: var(--r-md); display: inline-grid; place-items: center; font-weight: 700; flex: none;
    background: var(--surface-2); color: var(--muted);
  }
  .agent { background: var(--au-agent); color: var(--au-agent-ink); }
  .owner { background: var(--au-owner); color: var(--au-owner-ink); }
  .boss { background: var(--au-boss); color: var(--au-boss-ink); }
  .monitor { background: var(--au-monitor); color: var(--au-monitor-ink); }
</style>
