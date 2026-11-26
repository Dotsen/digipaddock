<script lang="ts" module>
  export type Mode = "now" | "batch" | "feed";
  export const MODES: Mode[] = ["now", "batch", "feed"];
</script>

<script lang="ts">
  // Notification rules of the person: one row per kind, a radio per mode (now, every 30 minutes, the feed
  // only). Controlled: the page saves the change and passes the stored rules back.
  import { hasKey, t } from "../lib/i18n.svelte";

  let {
    kinds,
    onchange,
    disabled = false,
  }: { kinds: Record<string, Mode>; onchange: (kind: string, mode: Mode) => void; disabled?: boolean } = $props();

  const label = (kind: string) => {
    const key = `notify.kind.${kind}`;
    return hasKey(key) ? t(key) : kind;
  };
</script>

<div class="wrap">
  <table class="prefs">
    <thead>
      <tr>
        <th scope="col"></th>
        {#each MODES as mode (mode)}<th scope="col">{t(`notify.mode.${mode}`)}</th>{/each}
      </tr>
    </thead>
    <tbody>
      {#each Object.keys(kinds) as kind (kind)}
        <tr>
          <th scope="row">{label(kind)}</th>
          {#each MODES as mode (mode)}
            <td>
              <input
                type="radio"
                name="pref-{kind}"
                aria-label="{label(kind)}: {t(`notify.mode.${mode}`)}"
                checked={kinds[kind] === mode}
                {disabled}
                onchange={() => onchange(kind, mode)}
              />
            </td>
          {/each}
        </tr>
      {/each}
    </tbody>
  </table>
</div>

<style>
  .wrap { overflow-x: auto; }
  .prefs { border-collapse: collapse; width: 100%; font-size: var(--fs-ui); }
  .prefs th, .prefs td { padding: var(--sp-8) var(--sp-6); border-bottom: 1px solid var(--line-soft); }
  .prefs thead th { font-size: var(--fs-sm); color: var(--muted); font-weight: 500; text-align: center; }
  .prefs tbody th { text-align: left; font-weight: 400; }
  .prefs td { text-align: center; width: 84px; }
  .prefs input { width: 18px; height: 18px; accent-color: var(--accent); }
</style>
