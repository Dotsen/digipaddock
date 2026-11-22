<script lang="ts">
  // Projects, with `settings`: the list, a new project (its id comes from the name, shown and editable
  // until saved, then never changes), a new name or route, and a delete that names the cards that move, the
  // people whose roles lose the project and the project that takes the cards, confirmed by a step-up.
  // A repository: owner/repo is checked here and sent as a request; the root unit's outcome comes back
  // as a project event and the list reads it again in place.
  // A memory bank: the Hindsight bank of the sessions of the project's cards, saved with the name and
  // route;
  // empty — the assignee's role bank. A host without Hindsight shows no bank: there is no memory to bind.
  // Who closes Done: the agent that closes the project's cards itself, one of the routes' agents; empty —
  // the owner's acceptance.
  import { onMount } from "svelte";
  import BackButton from "../components/BackButton.svelte";
  import { apiDelete, apiGet, apiPatch, apiPost } from "../lib/api";
  import { errorText, t, type Key } from "../lib/i18n.svelte";
  import { validBank } from "../lib/memory"; // checked trimmed, so no pattern attribute on the field
  import { REPO_PATTERN, repoWaiting, validRepo } from "../lib/repo";
  import { session } from "../lib/session.svelte";
  import { ID_MAX, ID_PATTERN, slug } from "../lib/slug";
  import { confirmed } from "../lib/stepup-dialog.svelte";
  import CouncilEditor from "../components/CouncilEditor.svelte";
  import { same, type Council } from "../lib/council";
  import { models } from "../lib/stores/models.svelte";
  import { projects } from "../lib/stores/projects.svelte";
  import { toast } from "../lib/toast.svelte";
  import { EFFORTS, type Project, type ProjectImpact } from "../lib/types";
  import { isCancelled } from "../lib/webauthn";

  const settings = $derived(session.can("settings"));
  const memory = $derived(!!session.me?.memory);
  const NAME_MAX = 60;

  let routes = $state<string[]>([]);
  let listed = $state(false);
  let busy = $state("");

  // the new project
  let name = $state("");
  let id = $state("");
  let idEdited = $state(false); // the person typed an id: the name no longer rewrites it
  let route = $state("");
  let repo = $state("");

  // one project opened for a change or a delete
  let open = $state("");
  let mode = $state<"edit" | "delete">("edit");
  let editName = $state("");
  let editRoute = $state("");
  let editRepo = $state("");
  let editBank = $state("");
  let editCloser = $state("");
  let editModel = $state(""); // the model and effort of the project's cards' sessions by default
  let editEffort = $state("");
  let editCouncil = $state<Council>({}); // the default council of the project's cards
  let councilValid = $state(true);
  let impact = $state<ProjectImpact | null>(null);
  let target = $state("");

  const failed = (exc: unknown) => toast(isCancelled(exc) ? t("error.passkey_cancelled") : errorText(exc), "error");
  const shownId = $derived(idEdited ? id : slug(name));
  const others = $derived(projects.list.filter((p) => p.id !== open));
  const closers = $derived(routes.filter((r) => r !== "@owner").map((r) => r.slice(1)));
  // the enabled models, and the one the project is on even if disabled since (the select keeps its value)
  const modelOptions = $derived(models.list.filter((m) => m.enabled || m.id === editModel));
  const routeText = (value: string) => (value === "@owner" ? t("projects.route.owner") : value);
  const repoState = (project: Project) =>
    project.repo_state ? t(`projects.repo_state.${project.repo_state}` as Key) : "";
  const repoFailed = (project: Project) => ["no_access", "no_key", "hooks", "error"].includes(project.repo_state ?? "");

  onMount(() => {
    if (!settings) return;
    projects.ensure().then(() => (listed = true), (exc) => {
      listed = true; // the toast says why the list is empty
      failed(exc);
    });
    models.ensure().catch(failed);
    apiGet<string[]>("/api/projects/routes").then((found) => {
      routes = found;
      route ||= found.find((r) => r !== "@owner") ?? found[0] ?? "";
    }, failed);
  });

  async function run(step: string, work: () => Promise<unknown>) {
    busy = step;
    try {
      await work();
    } catch (exc) {
      failed(exc);
    } finally {
      busy = "";
    }
  }

  const create = (event: SubmitEvent) => {
    event.preventDefault();
    return run("create", async () => {
      const made = await apiPost<Project>("/api/projects", { name: name.trim(), id: shownId, route });
      const wanted = repo.trim();
      name = "";
      id = "";
      idEdited = false;
      repo = "";
      toast(t("projects.created", { name: made.name, id: made.id }), "info");
      try {
        if (wanted) {
          await apiPost<Project>(`/api/projects/${encodeURIComponent(made.id)}/repo`, { repo: wanted });
          toast(t("projects.repo_requested"), "info");
        }
      } catch (exc) {
        // the project exists: its own editor keeps the repository for another try
        edit(made);
        editRepo = wanted;
        throw exc;
      } finally {
        await projects.refresh();
      }
    });
  };

  const bindRepo = (project: Project) =>
    run(`repo-${project.id}`, async () => {
      await apiPost<Project>(`/api/projects/${encodeURIComponent(project.id)}/repo`, { repo: editRepo.trim() });
      await projects.refresh();
      toast(t("projects.repo_requested"), "info");
    });

  const unbindRepo = (project: Project) =>
    run(`repo-${project.id}`, async () => {
      await apiDelete<Project>(`/api/projects/${encodeURIComponent(project.id)}/repo`);
      await projects.refresh();
      toast(t("projects.repo_requested"), "info");
    });

  function close() {
    open = "";
    impact = null;
    target = "";
  }

  function edit(project: Project) {
    if (open === project.id && mode === "edit") return close();
    close();
    open = project.id;
    mode = "edit";
    editName = project.name;
    editRoute = project.route;
    editRepo = project.repo ?? "";
    editBank = project.memory_bank ?? "";
    editCloser = project.done_by ?? "";
    editModel = project.model ?? "";
    editEffort = project.effort ?? "";
    editCouncil = project.council ?? {};
    councilValid = true;
  }

  const save = (project: Project) =>
    run(`save-${project.id}`, async () => {
      const change: Record<string, unknown> = {};
      if (editName.trim() !== project.name) change.name = editName.trim();
      if (editRoute !== project.route) change.route = editRoute;
      if (editBank.trim() !== (project.memory_bank ?? "")) change.memory_bank = editBank.trim();
      if (editCloser !== (project.done_by ?? "")) change.done_by = editCloser;
      if (editModel !== (project.model ?? "")) change.model = editModel;
      if (editEffort !== (project.effort ?? "")) change.effort = editEffort;
      if (!same(editCouncil, project.council ?? {})) change.council = editCouncil;
      if (Object.keys(change).length) {
        await apiPatch<Project>(`/api/projects/${encodeURIComponent(project.id)}`, change);
        await projects.refresh();
        toast(t("settings.saved"), "info");
      }
      close();
    });

  function askDelete(project: Project) {
    if (open === project.id && mode === "delete") return close();
    close();
    open = project.id;
    mode = "delete";
    void run(`impact-${project.id}`, async () => {
      try {
        const found = await apiGet<ProjectImpact>(`/api/projects/${encodeURIComponent(project.id)}/impact`);
        if (open === project.id && mode === "delete") impact = found;
      } catch (exc) {
        if (open === project.id && mode === "delete") close(); // no facts, no delete: the toast says why
        throw exc;
      }
    });
  }

  const remove = (project: Project) =>
    run(`delete-${project.id}`, async () => {
      const done = await confirmed("projects.delete", (stepup) =>
        apiDelete<{ moved: number; target: string; grants_error?: string }>(
          `/api/projects/${encodeURIComponent(project.id)}`,
          { target, stepup },
        ),
      );
      close();
      await projects.refresh();
      toast(t("projects.deleted", { name: project.name, moved: done.moved, target: projects.name(done.target) }), "info");
      if (done.grants_error) toast(t("projects.grants_error"), "error");
    });
</script>

<div class="topbar">
  <a class="back" href="/settings" aria-label={t("nav.settings")}>←</a>
  <BackButton />
  <h1>{t("settings.projects")}</h1>
</div>

{#if !settings}
  <p class="empty">{t("error.forbidden")}</p>
{:else}
  <ul class="projects">
    {#each projects.list as project (project.id)}
      <li class="panel">
        <div class="head">
          <span class="who">
            <b>{project.name}</b>
            <span class="muted small"><code>{project.id}</code> · {routeText(project.route)}{#if project.repo}{" · "}{project.repo}{/if}</span>
            {#if project.repo_state}
              <span class="small repo-state" class:warn={repoFailed(project)}>
                {t("projects.repo")}: {repoState(project)}{#if project.repo_detail}{" — "}{project.repo_detail}{/if}
              </span>
            {/if}
          </span>
          <button class="btn small" type="button" aria-expanded={open === project.id && mode === "edit"} onclick={() => edit(project)}>
            {t("action.edit")}
          </button>
          <button class="btn small" type="button" aria-expanded={open === project.id && mode === "delete"} onclick={() => askDelete(project)}>
            {t("action.remove")}
          </button>
        </div>

        {#if open === project.id && mode === "edit"}
          <form
            class="editor stack"
            onsubmit={(e) => {
              e.preventDefault();
              void save(project);
            }}
          >
            <label class="field"><span>{t("projects.name")}</span><input type="text" required maxlength={NAME_MAX} bind:value={editName} /></label>
            <label class="field">
              <span>{t("projects.route")}</span>
              <select bind:value={editRoute}>
                {#if !routes.includes(project.route)}<option value={project.route}>{routeText(project.route)}</option>{/if}
                {#each routes as r (r)}<option value={r}>{routeText(r)}</option>{/each}
              </select>
            </label>
            <p class="muted small">{t("projects.route_hint")}</p>
            {#if memory}
              <label class="field">
                <span>{t("projects.memory_bank")}</span>
                <input
                  type="text"
                  maxlength={110}
                  autocapitalize="off"
                  spellcheck="false"
                  bind:value={editBank}
                />
              </label>
              {#if !validBank(editBank.trim())}<p class="warn small">{t("projects.memory_bank_invalid")}</p>{/if}
              <p class="muted small">{t("projects.memory_bank_hint")}</p>
            {/if}
            <label class="field">
              <span>{t("projects.done_by")}</span>
              <select bind:value={editCloser}>
                <option value="">{t("projects.done_by_owner")}</option>
                {#if project.done_by && !closers.includes(project.done_by)}<option value={project.done_by}>@{project.done_by}</option>{/if}
                {#each closers as c (c)}<option value={c}>@{c}</option>{/each}
              </select>
            </label>
            <p class="muted small">{t("projects.done_by_hint")}</p>
            <label class="field">
              <span>{t("projects.model")}</span>
              <select bind:value={editModel}>
                <option value="">{t("projects.model_subscription")}</option>
                {#each modelOptions as m (m.id)}<option value={m.id}>{models.title(m)}</option>{/each}
              </select>
            </label>
            <label class="field">
              <span>{t("projects.effort")}</span>
              <select bind:value={editEffort}>
                <option value="">{t("projects.effort_agent")}</option>
                {#each EFFORTS as level (level)}<option value={level}>{level}</option>{/each}
              </select>
            </label>
            <p class="muted small">{t("projects.model_hint")}</p>
            <div class="council">
              <span class="label">{t("projects.council")}</span>
              <CouncilEditor
                value={project.council ?? {}}
                onchange={(council, valid) => {
                  editCouncil = council;
                  councilValid = valid;
                }}
              />
              <p class="muted small">{t("projects.council_hint")}</p>
            </div>
            <div class="row">
              <button
                class="btn small primary"
                type="submit"
                disabled={busy === `save-${project.id}` || !editName.trim() || !validBank(editBank.trim()) || !councilValid}
              >{t("action.save")}</button>
              <button class="btn small ghost" type="button" onclick={close}>{t("action.cancel")}</button>
            </div>
          </form>
          <!-- outside the form: Enter here never saves the name or the route -->
          <div class="repo stack" role="group" aria-label={t("projects.repo")}>
            <label class="field">
              <span>{t("projects.repo")}</span>
              <input
                type="text"
                placeholder="owner/repo"
                pattern={REPO_PATTERN}
                autocapitalize="off"
                spellcheck="false"
                disabled={project.repo_release || repoWaiting(project.repo_state)}
                bind:value={editRepo}
                onkeydown={(e) => {
                  if (e.key !== "Enter" || busy || !validRepo(editRepo.trim()) || repoWaiting(project.repo_state)) return;
                  void bindRepo(project);
                }}
              />
            </label>
            {#if editRepo.trim() && !validRepo(editRepo.trim())}<p class="warn small">{t("projects.repo_invalid")}</p>{/if}
            {#if project.repo_release}
              <p class="muted small">{t("projects.repo_release")}</p>
            {:else}
              <p class="muted small">{t("projects.repo_hint")}</p>
              <div class="row">
                <button
                  class="btn small"
                  type="button"
                  disabled={!validRepo(editRepo.trim()) || repoWaiting(project.repo_state) || busy === `repo-${project.id}`}
                  onclick={() => bindRepo(project)}
                >
                  {t("projects.repo_bind")}
                </button>
                {#if project.repo}
                  <button
                    class="btn small ghost"
                    type="button"
                    disabled={repoWaiting(project.repo_state) || busy === `repo-${project.id}`}
                    onclick={() => unbindRepo(project)}
                  >
                    {t("projects.repo_unbind")}
                  </button>
                {/if}
              </div>
            {/if}
          </div>
        {:else if open === project.id && mode === "delete"}
          <div class="editor stack" role="group" aria-label={t("projects.delete_title", { name: project.name })}>
            <b>{t("projects.delete_title", { name: project.name })}</b>
            {#if impact === null}
              <p class="muted">{t("app.loading")}</p>
            {:else if impact.bound}
              <p class="warn">{t("projects.bound")}</p>
            {:else}
              <p>{t("projects.delete_cards", { count: impact.cards })}</p>
              <p>
                {t("projects.delete_users")}
                {impact.users.length ? impact.users.map((u) => u.name).join(", ") : "—"}
              </p>
              <label class="field">
                <span>{t("projects.target")}</span>
                <select bind:value={target} required>
                  <option value="" disabled>—</option>
                  {#each others as p (p.id)}<option value={p.id}>{p.name}</option>{/each}
                </select>
              </label>
              <p class="muted small">{t("projects.delete_hint")}</p>
              <div class="row">
                <button
                  class="btn small danger"
                  type="button"
                  disabled={!target || busy === `delete-${project.id}`}
                  onclick={() => remove(project)}
                >
                  {t("projects.delete_confirm")}
                </button>
                <button class="btn small ghost" type="button" onclick={close}>{t("action.cancel")}</button>
              </div>
            {/if}
          </div>
        {/if}
      </li>
    {:else}
      {#if !listed}<li class="empty">{t("app.loading")}</li>{/if}
    {/each}
  </ul>

  <h2 class="section-h">{t("projects.add")}</h2>
  <form class="panel stack" onsubmit={create}>
    <label class="field"><span>{t("projects.name")}</span><input type="text" required maxlength={NAME_MAX} bind:value={name} /></label>
    <label class="field">
      <span>{t("projects.id")}</span>
      <input
        type="text"
        required
        maxlength={ID_MAX}
        pattern={ID_PATTERN}
        autocapitalize="off"
        spellcheck="false"
        value={shownId}
        oninput={(e) => {
          id = e.currentTarget.value;
          idEdited = id !== ""; // cleared: the name gives the id again
        }}
      />
    </label>
    <p class="muted small">{t("projects.id_hint")}</p>
    <label class="field">
      <span>{t("projects.route")}</span>
      <select bind:value={route} required>
        {#each routes as r (r)}<option value={r}>{routeText(r)}</option>{/each}
      </select>
    </label>
    <label class="field">
      <span>{t("projects.repo")}</span>
      <input type="text" placeholder="owner/repo" pattern={REPO_PATTERN} autocapitalize="off" spellcheck="false" bind:value={repo} />
    </label>
    {#if repo.trim() && !validRepo(repo.trim())}<p class="warn small">{t("projects.repo_invalid")}</p>{/if}
    <p class="muted small">{t("projects.repo_hint")}</p>
    <div>
      <button
        class="btn primary"
        type="submit"
        disabled={busy === "create" || !name.trim() || !shownId || !route || (!!repo.trim() && !validRepo(repo.trim()))}
      >
        {t("action.create")}
      </button>
    </div>
  </form>
{/if}

<style>
  .back { color: var(--muted); text-decoration: none; font-size: 20px; }
  @media (min-width: 900px) { .back { display: none; } } /* «Назад» there */
  .projects { list-style: none; margin: 0 0 8px; padding: 0; display: grid; gap: 8px; }
  .head { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .who { flex: 1 1 200px; display: grid; min-width: 0; }
  .small { font-size: 12px; }
  .editor { margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--line-soft); }
  .editor p { margin: 0; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .warn { color: var(--crit); }
  .repo { margin-top: 4px; padding-top: 12px; border-top: 1px solid var(--line-soft); }
  .repo-state:not(.warn) { color: var(--muted); }
  p.small { margin: 0; }
  .btn.danger { color: var(--crit); border-color: var(--crit); }
</style>
