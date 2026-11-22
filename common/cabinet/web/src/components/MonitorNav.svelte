<script lang="ts">
  import { t } from "../lib/i18n.svelte";
  let { active }: { active: "servers" | "incidents" } = $props();
</script>

<nav class="monitor-nav" aria-label={t("mon.navigation")}>
  <a class="btn" href="/servers" aria-current={active === "servers" ? "page" : undefined}>{t("nav.servers")}</a>
  <a class="btn" href="/incidents" aria-current={active === "incidents" ? "page" : undefined}>{t("nav.incidents")}</a>
</nav>
