<script lang="ts">
  // "Back" left of a page's title on a desktop — the tab's previous page inside the cabinet, or the
  // page's parent (router.parentOf). A phone keeps the page's own ← and the system's back.
  import { t } from "../lib/i18n.svelte";
  import { goBack } from "../lib/router.svelte";
</script>

<button type="button" class="iconbtn back-btn" aria-label={t("nav.back")} title={t("nav.back")} onclick={goBack}>
  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 5l-7 7 7 7" /></svg>
</button>

<style>
  .back-btn { display: none; flex: none; }
  @media (min-width: 900px) {
    .back-btn { display: grid; }
  }
</style>
