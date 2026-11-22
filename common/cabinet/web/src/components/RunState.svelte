<script module lang="ts">
  import type { AgentSession as Session } from "../lib/types";

  /** A younger heartbeat confirms work: the server's ``team.FRESH``, so the card says what the Overview says. */
  export const FRESH_MS = 120_000;

  /** The session starts or works and its host still says so: its heartbeat (none yet: its last update) is fresh.
   * A saved «working» alone confirms nothing — a host that went silent leaves it behind. */
  export function beats(session: Session | undefined, now: number): boolean {
    if (!session || (session.state !== "working" && session.state !== "starting")) return false;
    return now - Date.parse(session.heartbeat || session.updated) <= FRESH_MS;
  }

  /** The agent whose session on the card is at work or starting, its heartbeat fresh, as "@agent";
   * "" when none is. */
  export function workingAgent(sessions: Session[], now: number): string {
    const busy = sessions.find((s) => beats(s, now));
    return busy ? `@${busy.agent}` : "";
  }
</script>

<script lang="ts">
  // Under the card's header: how far the newest request to its agent got — saved and queued,
  // handed over (or waiting for a free slot, or a start that is late), at work, waiting for the person's
  // answer, failed with «retry». A stop is not the card's run: the request before it is shown. «At work» is
  // only what a fresh heartbeat confirms; without one it is «no heartbeat», as on the Overview.
  import { t } from "../lib/i18n.svelte";
  import { reasonText } from "../lib/runReason";
  import { age, stamp } from "../lib/time";
  import type { AgentSession, Command, OperationPause } from "../lib/types";

  let {
    commands,
    sessions,
    waiting = false,
    canControl = false,
    now = Date.now(),
    pauses = [],
    oncontinue,
  }: {
    /** Newest first, as the server lists them. */
    commands: Command[];
    sessions: AgentSession[];
    /** The agent asked the person a question on the card that is still open. */
    waiting?: boolean;
    canControl?: boolean;
    now?: number;
    /** The roles a factory operation holds now: a request of such a role waits for it, not for a slot. */
    pauses?: OperationPause[];
    oncontinue?: (session: string) => void;
  } = $props();

  const DELAYED_MS = 30_000;
  const command = $derived(commands.find((c) => c.kind !== "stop"));
  const agent = $derived(command ? `@${command.agent}` : "");
  const session = $derived(command ? sessions.find((s) => s.id === command.session) : undefined);
  const alive = $derived(beats(session, now));
  const pause = $derived(command ? pauses.find((p) => p.agent === `@${command.agent}`) : undefined);
  function paused(p: OperationPause): string {
    if (p.held === "failed") return t("run.paused.failed", { agent, card: p.card || "—" });
    const busy = p.busy.map((b) => (b.card ? t("run.paused.turn", { agent: b.agent, card: b.card }) : b.agent)).join(", ");
    if (!busy) return t("run.paused.installing", { agent, card: p.card || "—" });
    return t("run.paused", { agent, card: p.card || "—", busy, until: p.until ? stamp(p.until, now) : "—" });
  }
  // how long the request waits and how many are ahead of it — a place in a queue, never a time it means
  function slot(s: AgentSession): string {
    const parts = [t("run.slot")];
    if (s.queue_position) parts.push(s.queue_position === 1 ? t("run.slot.first") : t("run.slot.ahead", { n: s.queue_position - 1 }));
    if (s.queued_since) parts.push(t("run.waits", { age: age(s.queued_since, now) }));
    return parts.join(" · ");
  }
  function behind(c: Command): string {
    // the thread's requests the host holds before this one; the running turn is the agent's, not counted
    const ahead = commands.filter((o) => o.session === c.session && o.state === "delivered" && o.created < c.created).length;
    const parts = [ahead ? t("run.behind", { agent, n: ahead }) : t("run.next", { agent })];
    parts.push(t("run.waits", { age: age(c.updated, now) }));
    return parts.join(" · ");
  }
  const line = $derived.by<{ text: string; tone: string; title?: string; busy?: boolean } | null>(() => {
    if (!command) return null;
    const stale = { text: t("run.stale", { agent }), tone: "warn" };
    const held = pause ? { text: paused(pause), tone: "info", title: pause.operation } : null;
    switch (command.state) {
      case "queued":
        return held ?? { text: t("run.queued"), tone: "" };
      case "delivered":
        if (held && session?.state !== "working") return held;
        if (session?.state === "queued") return { text: slot(session), tone: "" };
        // the session runs another turn of this thread: this request waits behind it, not late
        if (session?.state === "working") return alive ? { text: behind(command), tone: "good", busy: true } : stale;
        if (now - Date.parse(command.updated) > DELAYED_MS) return { text: t("run.delayed"), tone: "warn" };
        return { text: t("run.delivered", { agent }), tone: "info" };
      case "started":
        return alive ? { text: t("run.started", { agent }), tone: "good", busy: true } : stale;
      case "done":
        return waiting ? { text: t("run.waiting"), tone: "warn" } : null;
      case "failed": {
        const code = command.reason || command.error;
        return { text: t("run.failed", { reason: reasonText(code) }), tone: "crit", title: code };
      }
      default:
        return null;
    }
  });
</script>

{#if line && command}
  <div class="run" role="status">
    <span class="chip {line.tone}" title={line.title}>{#if line.busy}<span class="spin" aria-hidden="true"></span>{/if}{line.text}</span>
    {#if command.state === "failed" && canControl && command.session && oncontinue}
      <button type="button" class="btn small" onclick={() => oncontinue(command.session)}>{t("run.retry")}</button>
    {/if}
  </div>
{/if}

<style>
  .run { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; font-size: 13px; }
  .run .chip { white-space: normal; display: inline-flex; gap: 6px; align-items: center; }
  .spin { width: 10px; height: 10px; flex: none; border: 2px solid currentColor; border-top-color: transparent; border-radius: 50%; animation: spin 0.8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
</style>
