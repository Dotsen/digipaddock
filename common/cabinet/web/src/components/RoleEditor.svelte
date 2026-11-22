<script lang="ts">
  // The permissions of one role: a checkbox per permission of the finite catalog, or «all» (`*`, the
  // owner's). The page saves through a step-up; the editor only says what was ticked.
  import { t } from "../lib/i18n.svelte";

  let {
    id,
    perms,
    all,
    onsave,
  }: { id: string; perms: string[]; all: string[]; onsave: (perms: string[]) => Promise<void> } = $props();

  // svelte-ignore state_referenced_locally
  let chosen = $state(new Set(perms));
  let saving = $state(false);
  const everything = $derived(chosen.has("*"));
  const ordered = $derived(everything ? ["*"] : all.filter((p) => chosen.has(p)));
  const changed = $derived(ordered.join(",") !== [...perms].sort(byCatalog).join(","));

  function byCatalog(a: string, b: string) {
    return (a === "*" ? -1 : all.indexOf(a)) - (b === "*" ? -1 : all.indexOf(b));
  }

  function toggle(perm: string) {
    const next = new Set(chosen);
    if (next.has(perm)) next.delete(perm);
    else next.add(perm);
    chosen = next;
  }

  async function save() {
    saving = true;
    try {
      await onsave(ordered);
    } finally {
      saving = false;
    }
  }
</script>

<fieldset class="role">
  <legend>{id}</legend>
  <label class="perm all">
    <input type="checkbox" checked={everything} onchange={() => toggle("*")} />
    {t("roles.all")}
  </label>
  <div class="grid">
    {#each all as perm (perm)}
      <label class="perm">
        <input type="checkbox" checked={everything || chosen.has(perm)} disabled={everything} onchange={() => toggle(perm)} />
        <code>{perm}</code>
      </label>
    {/each}
  </div>
  <div>
    <button class="btn small primary" type="button" disabled={!changed || saving} onclick={save}>{t("action.save")}</button>
  </div>
</fieldset>

<style>
  .role { border: 1px solid var(--line-soft); border-radius: 10px; padding: 10px 12px; display: grid; gap: 8px; margin: 0; }
  legend { padding: 0 6px; font-weight: 600; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 4px 12px; }
  .perm { display: flex; gap: 6px; align-items: center; font-size: 13px; }
  .perm code { font-family: var(--font-mono); font-size: 12px; }
  .perm.all { font-weight: 500; }
  input { accent-color: var(--accent); }
</style>
