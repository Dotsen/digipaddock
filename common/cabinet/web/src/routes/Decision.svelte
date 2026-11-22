<script lang="ts">
  // /decisions/<id> (a push's link) and /decisions/by-proposal/<proposal> (the old BOSS page's links): the
  // decision lives on its card's page — go there with it highlighted. A decision without a card (the monitor's
  // trust) is shown and decided right here.
  import BackButton from "../components/BackButton.svelte";
  import DecisionCard from "../components/DecisionCard.svelte";
  import { ApiError, apiGet } from "../lib/api";
  import { errorText, t, type Key } from "../lib/i18n.svelte";
  import { navigate } from "../lib/router.svelte";
  import type { DecisionItem } from "../lib/types";

  let { id, proposal }: { id?: string; proposal?: string } = $props();
  let error = $state("");
  let item = $state<DecisionItem | null>(null);

  async function find(): Promise<DecisionItem> {
    if (id) return apiGet<DecisionItem>(`/api/decisions/${encodeURIComponent(id)}`);
    const found = await apiGet<DecisionItem[]>(`/api/decisions?state=all&proposal=${encodeURIComponent(proposal ?? "")}`);
    if (!found.length) throw new ApiError(404, "not_found", "no decision for this proposal");
    return found[0];
  }

  function load() {
    find().then(
      (found) => {
        if (found.decision.card) navigate(`/cards/${found.decision.card}?decision=${found.decision.id}`, { replace: true });
        else item = found;
      },
      (exc) => (error = errorText(exc)),
    );
  }

  $effect(() => {
    void [id, proposal];
    load();
  });
</script>

{#if error}
  <div class="topbar"><BackButton /><h1>{error}</h1></div>
{:else if item}
  <div class="topbar"><BackButton /><h1>{t(`decision.type.${item.decision.type}` as Key)}</h1></div>
  <DecisionCard {item} highlight onchanged={load} />
{:else}
  <p class="empty">{t("app.loading")}</p>
{/if}
