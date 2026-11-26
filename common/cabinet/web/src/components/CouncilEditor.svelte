<script lang="ts">
  // The council's composition, edited per kind — the brief and the code: the critics (any enabled model;
  // one named twice reads twice, independently; at most ten), the rounds and, for the code, the least diff in
  // changed lines (0 — every pull request). The critics are picked at once, from a list with search: a
  // kind without critics is simply not there. Only on a card whose project names critics for a kind is there a
  // choice first — the project's, the card's own or none at all. With `onsave` the editor has its own "Save";
  // with `onchange` it reports every edit to the form around it, and whether the numbers are in range.
  import { untrack } from "svelte";
  import {
    CRITICS_MAX, KINDS, MIN_LINES_DEFAULT, MIN_LINES_MAX, ROUNDS_DEFAULT, ROUNDS_MAX,
    type Council, type CouncilKind, type CouncilPart,
  } from "../lib/council";
  import { t } from "../lib/i18n.svelte";
  import { models } from "../lib/stores/models.svelte";
  import Picker from "./Picker.svelte";

  let {
    value,
    inherited,
    saving = false,
    onsave,
    onchange,
  }: {
    value: Council;
    inherited?: Council;
    saving?: boolean;
    onsave?: (council: Council) => void;
    onchange?: (council: Council, valid: boolean) => void;
  } = $props();

  type Mode = "project" | "own" | "off";
  interface Draft {
    mode: Mode;
    critics: string[];
    rounds: number | null; // a number input's bound value: null while empty
    min_lines: number | null;
  }

  /** The card's project names critics for the kind: only then is there something to take, or to switch off. */
  const chooses = (kind: CouncilKind) => (inherited?.[kind]?.critics.length ?? 0) > 0;
  // the editor starts from the composition it was opened with; what it holds since is the person's draft
  const draft = (kind: CouncilKind): Draft => {
    const part = value[kind];
    return {
      mode: !part ? "project" : part.critics.length ? "own" : "off", // read only where the kind `chooses`
      critics: [...(part?.critics ?? [])],
      rounds: part?.rounds ?? ROUNDS_DEFAULT,
      min_lines: part?.min_lines ?? MIN_LINES_DEFAULT,
    };
  };
  let drafts = $state<Record<CouncilKind, Draft>>({ brief: draft("brief"), code: draft("code") });

  const options = $derived(models.list.filter((m) => m.enabled).map((m) => ({ value: m.id, text: models.title(m) })));
  const whole = (n: number | null, low: number, high: number): n is number =>
    typeof n === "number" && Number.isInteger(n) && n >= low && n <= high;
  /** The kind's critics are picked here: always, but for a kind left to the project or switched off. */
  const picks = (kind: CouncilKind) => !chooses(kind) || drafts[kind].mode === "own";
  /** The critics the kind is saved with. */
  const named = (kind: CouncilKind) => (picks(kind) ? drafts[kind].critics : []);
  const valid = $derived(
    KINDS.every((kind) => {
      const d = drafts[kind];
      return !named(kind).length || (whole(d.rounds, 1, ROUNDS_MAX) && (kind !== "code" || whole(d.min_lines, 0, MIN_LINES_MAX)));
    }),
  );
  const inheritedText = (kind: CouncilKind) => {
    const count = inherited?.[kind]?.critics.length ?? 0;
    return count ? String(count) : t("council.none");
  };

  /** The composition as the API holds it: a kind «as the project's» or without critics is left out; «none» on a
   * card whose project names critics is the kind without critics. */
  function result(): Council {
    const out: Council = {};
    for (const kind of KINDS) {
      const d = drafts[kind];
      const critics = named(kind);
      const given = value[kind];
      if (chooses(kind) ? d.mode === "project" : !critics.length) {
        // a «none» the card held before its project had anything to switch off stays as it was: no write
        if (!chooses(kind) && given && !given.critics.length) out[kind] = given;
        continue;
      }
      const part: CouncilPart = {
        critics: [...critics],
        rounds: critics.length && whole(d.rounds, 1, ROUNDS_MAX) ? d.rounds : ROUNDS_DEFAULT,
      };
      if (kind === "code") part.min_lines = critics.length && whole(d.min_lines, 0, MIN_LINES_MAX) ? d.min_lines : MIN_LINES_DEFAULT;
      out[kind] = part;
    }
    return out;
  }

  $effect(() => {
    if (!onchange) return;
    const council = result();
    const ok = valid;
    untrack(() => onchange(council, ok));
  });
</script>

<div class="council">
  {#each KINDS as kind (kind)}
    <fieldset class="kind">
      <legend>{t(`council.${kind}`)}</legend>
      {#if chooses(kind)}
        <label class="field">
          <span>{t("council.mode")}</span>
          <select bind:value={drafts[kind].mode}>
            <option value="project">{t("council.project", { summary: inheritedText(kind) })}</option>
            <option value="own">{t("council.own")}</option>
            <option value="off">{t("council.off")}</option>
          </select>
        </label>
      {/if}
      {#if picks(kind)}
        <div class="field">
          <span>{t("council.critic")}</span>
          <!-- a pick adds a critic and the list stays open: the same model picked again is one more read -->
          <Picker
            label={t("council.critic")}
            {options}
            value={drafts[kind].critics}
            repeat
            max={CRITICS_MAX}
            chosenText={(id) => models.name(id)}
            onchange={(critics) => (drafts[kind].critics = critics)}
          />
        </div>
        {#if drafts[kind].critics.length}
          <div class="numbers">
            <label class="field">
              <span>{t("council.rounds")}</span>
              <input type="number" min="1" max={ROUNDS_MAX} step="1" required bind:value={drafts[kind].rounds} />
            </label>
            {#if kind === "code"}
              <label class="field">
                <span>{t("council.min_lines")}</span>
                <input type="number" min="0" max={MIN_LINES_MAX} step="1" required bind:value={drafts[kind].min_lines} />
              </label>
            {/if}
          </div>
          {#if kind === "code"}<p class="muted small">{t("council.min_lines_hint")}</p>{/if}
        {/if}
      {/if}
    </fieldset>
  {/each}
  <p class="muted small">{t("council.same_model_hint")}</p>
  {#if onsave}
    <div>
      <button type="button" class="btn small primary" disabled={saving || !valid} onclick={() => onsave(result())}>{t("action.save")}</button>
    </div>
  {/if}
</div>

<style>
  .council { display: grid; gap: var(--sp-10); min-width: 0; }
  .kind { margin: 0; padding: var(--sp-10) var(--sp-12) var(--sp-12); border: 1px solid var(--line-soft); border-radius: 10px; display: grid; gap: var(--sp-8); min-width: 0; }
  .kind legend { padding: 0 var(--sp-6); font-size: var(--fs-xs); text-transform: uppercase; letter-spacing: .08em; color: var(--muted); }
  .numbers { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: var(--sp-8); }
  .small { font-size: var(--fs-sm); }
  p.small { margin: 0; }
</style>
