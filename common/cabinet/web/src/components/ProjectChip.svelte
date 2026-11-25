<script lang="ts">
  // A project's chip — the one chip wherever a card or a decision says whose it is (a card's tile, the inbox, the
  // card's page): the project's name edged in its colour (lib/projectcolor.ts, app.css --project-N).
  import { projects } from "../lib/stores/projects.svelte";

  let { id }: { id: string } = $props();
</script>

<span class="chip project" style:--pc={projects.color(id) || null}>{projects.name(id)}</span>

<style>
  .project { border-color: var(--pc, var(--line)); }
</style>
