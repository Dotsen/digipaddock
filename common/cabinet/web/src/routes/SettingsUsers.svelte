<script lang="ts">
  // People and roles. With users.manage: add a partner (the sign-in code is shown once), change roles and
  // their projects, block, issue a new code, close every session. With settings: the permissions of each
  // role. Every change goes through a step-up.
  import { onMount } from "svelte";
  import Avatar from "../components/Avatar.svelte";
  import BackButton from "../components/BackButton.svelte";
  import LoginCode from "../components/LoginCode.svelte";
  import RoleEditor from "../components/RoleEditor.svelte";
  import { apiGet, apiPatch, apiPost, apiPut } from "../lib/api";
  import { errorText, t } from "../lib/i18n.svelte";
  import { session } from "../lib/session.svelte";
  import { confirmed } from "../lib/stepup-dialog.svelte";
  import { projects } from "../lib/stores/projects.svelte";
  import { age } from "../lib/time";
  import { toast } from "../lib/toast.svelte";
  import type { Person, Scope } from "../lib/types";
  import { isCancelled } from "../lib/webauthn";

  const manage = $derived(session.can("users.manage"));
  const settings = $derived(session.can("settings"));

  let people = $state<Person[] | null>(null);
  let roles = $state<Record<string, string[]>>({});
  let catalog = $state<string[]>([]);
  let open = $state(""); // the person whose editor is shown
  let draft = $state<[string, Scope][]>([]);
  let issued = $state<{ user: string; code: string; expires?: string } | null>(null);
  let busy = $state("");
  let armed = $state("");
  let newRole = $state("");

  // the add form
  let id = $state("");
  let name = $state("");
  let role = $state("partner");
  let scope = $state<Scope>([]);

  const failed = (exc: unknown) => toast(isCancelled(exc) ? t("error.passkey_cancelled") : errorText(exc), "error");
  const roleIds = $derived(Object.keys(roles));

  async function load() {
    try {
      if (manage) {
        const found = await apiGet<{ users: Person[]; roles: Record<string, string[]> }>("/api/users");
        people = found.users;
        roles = found.roles;
      }
      if (settings) {
        const found = await apiGet<{ roles: Record<string, string[]>; permissions: string[] }>("/api/roles");
        roles = found.roles;
        catalog = found.permissions;
      }
    } catch (exc) {
      failed(exc);
    }
  }

  onMount(() => {
    projects.ensure().catch(failed);
    void load();
  });

  async function run(step: string, work: () => Promise<unknown>) {
    busy = step;
    armed = "";
    try {
      await work();
    } catch (exc) {
      failed(exc);
    } finally {
      busy = "";
    }
  }

  function arm(step: string, work: () => Promise<unknown>) {
    if (armed !== step) armed = step;
    else void run(step, work);
  }

  const scopeText = (value: Scope) =>
    value === "*" ? t("users.all_projects") : value.map((p) => projects.name(p)).join(", ") || "—";
  const body = (assignments: [string, Scope][]) => assignments.map(([r, p]) => ({ role: r, projects: p }));

  function edit(person: Person) {
    open = open === person.id ? "" : person.id;
    draft = person.roles.map(([r, p]) => [r, p === "*" ? "*" : [...p]]);
    armed = "";
  }

  function toggleProject(current: Scope, project: string): Scope {
    const list = current === "*" ? [] : current;
    return list.includes(project) ? list.filter((p) => p !== project) : [...list, project];
  }

  const replace = (person: Person) => (people = (people ?? []).map((p) => (p.id === person.id ? person : p)));

  const saveRoles = (person: Person) =>
    run(`roles-${person.id}`, async () => {
      const changed = await confirmed("users.manage", (stepup) =>
        apiPatch<Person>(`/api/users/${person.id}`, { roles: body(draft), stepup }),
      );
      await load();
      open = changed.id === person.id ? "" : open;
      toast(t("settings.saved"), "info");
    });

  const setStatus = (person: Person, status: "active" | "blocked") =>
    arm(`status-${person.id}`, async () => {
      replace(
        await confirmed("users.manage", (stepup) =>
          apiPatch<Person>(`/api/users/${person.id}`, { status, stepup }),
        ),
      );
      await load();
    });

  const newCode = (person: Person) =>
    run(`code-${person.id}`, async () => {
      const { code, expires } = await confirmed("users.manage", (stepup) =>
        apiPost<{ code: string; expires: string }>(`/api/users/${person.id}/code`, { stepup }),
      );
      issued = { user: person.id, code, expires };
    });

  const closeSessions = (person: Person) =>
    arm(`out-${person.id}`, async () => {
      await confirmed("sessions.close", (stepup) => apiPost(`/api/users/${person.id}/logout-all`, { stepup }));
      toast(t("users.sessions_closed", { user: person.name }), "info");
    });

  const create = (event: SubmitEvent) => {
    event.preventDefault();
    return run("create", async () => {
      const made = await confirmed("users.manage", (stepup) =>
        apiPost<{ user: Person; code: string; expires: string }>("/api/users", {
          id: id.trim(),
          name: name.trim(),
          roles: [{ role, projects: scope }],
          stepup,
        }),
      );
      issued = { user: made.user.id, code: made.code, expires: made.expires };
      id = "";
      name = "";
      scope = [];
      await load();
    });
  };

  const saveRole = (roleId: string) => (perms: string[]) =>
    run(`role-${roleId}`, async () => {
      const saved = await confirmed("roles.set", (stepup) =>
        apiPut<{ roles: Record<string, string[]> }>(`/api/roles/${roleId}`, { perms, stepup }),
      );
      roles = saved.roles;
      newRole = "";
      toast(t("settings.saved"), "info");
    });

</script>

<div class="topbar">
  <a class="back" href="/settings" aria-label={t("nav.settings")}>←</a>
  <BackButton />
  <h1>{t("settings.users")}</h1>
</div>

{#if issued}
  <div class="issued">
    <LoginCode
      title={t("users.code_for", { user: issued.user })}
      code={issued.code}
      expires={issued.expires}
      onclose={() => (issued = null)}
    />
  </div>
{/if}

{#if !manage && !settings}
  <p class="empty">{t("error.forbidden")}</p>
{/if}

{#if manage}
  <h2 class="section-h">{t("users.people")}</h2>
  {#if people === null}
    <p class="empty">{t("app.loading")}</p>
  {:else}
    <ul class="people">
      {#each people as person (person.id)}
        <li class="panel">
          <div class="head">
            <Avatar name={person.name} kind="owner" size={32} />
            <span class="who">
              <b>{person.name}</b>
              <span class="muted small">@{person.id}{#if person.last_login}{" · "}{t("users.seen", { age: age(person.last_login) })}{/if}</span>
            </span>
            {#if person.status === "blocked"}<span class="chip crit">{t("users.blocked")}</span>{/if}
            <button class="btn small" type="button" aria-expanded={open === person.id} onclick={() => edit(person)}>{t("action.edit")}</button>
          </div>
          <div class="roles">
            {#each person.roles as [r, p] (r)}<span class="chip">{r}: {scopeText(p)}</span>{/each}
          </div>
          {#if open === person.id}
            <div class="editor stack">
              {#each draft as assignment, i (assignment[0])}
                <div class="assign">
                  <b>{assignment[0]}</b>
                  <label class="check">
                    <input type="checkbox" checked={assignment[1] === "*"} onchange={(e) => (draft[i][1] = e.currentTarget.checked ? "*" : [])} />
                    {t("users.all_projects")}
                  </label>
                  {#if assignment[1] !== "*"}
                    {#each projects.list as project (project.id)}
                      <label class="check">
                        <input
                          type="checkbox"
                          checked={assignment[1].includes(project.id)}
                          onchange={() => (draft[i][1] = toggleProject(draft[i][1], project.id))}
                        />
                        {project.name}
                      </label>
                    {/each}
                  {/if}
                  <button class="btn small ghost" type="button" aria-label={t("action.remove")} onclick={() => (draft = draft.filter((_, j) => j !== i))}>×</button>
                </div>
              {/each}
              <label class="field add-role">
                <span>{t("users.add_role")}</span>
                <select
                  value=""
                  onchange={(e) => {
                    const value = e.currentTarget.value;
                    if (value && !draft.some(([r]) => r === value)) draft = [...draft, [value, []]];
                    e.currentTarget.value = "";
                  }}
                >
                  <option value="">—</option>
                  {#each roleIds as r (r)}<option value={r}>{r}</option>{/each}
                </select>
              </label>
              <div class="row">
                <button class="btn small primary" type="button" disabled={busy === `roles-${person.id}`} onclick={() => saveRoles(person)}>{t("action.save")}</button>
                <button class="btn small" type="button" disabled={busy === `code-${person.id}`} onclick={() => newCode(person)}>{t("users.new_code")}</button>
                <button class="btn small" class:danger={armed === `out-${person.id}`} type="button" onclick={() => closeSessions(person)}>
                  {armed === `out-${person.id}` ? t("action.sure") : t("users.close_sessions")}
                </button>
                {#if person.id !== session.me?.id}
                  {#if person.status === "blocked"}
                    <button class="btn small" type="button" onclick={() => setStatus(person, "active")}>
                      {armed === `status-${person.id}` ? t("action.sure") : t("users.unblock")}
                    </button>
                  {:else}
                    <button class="btn small" class:danger={armed === `status-${person.id}`} type="button" onclick={() => setStatus(person, "blocked")}>
                      {armed === `status-${person.id}` ? t("action.sure") : t("users.block")}
                    </button>
                  {/if}
                {/if}
              </div>
            </div>
          {/if}
        </li>
      {/each}
    </ul>

    <h2 class="section-h">{t("users.add")}</h2>
    <form class="panel stack" onsubmit={create}>
      <div class="row">
        <label class="field grow">
          <span>{t("users.id")}</span>
          <input type="text" required pattern={"[a-z][a-z0-9_\\-]{0,31}"} autocapitalize="off" spellcheck="false" bind:value={id} />
        </label>
        <label class="field grow"><span>{t("settings.name")}</span><input type="text" required maxlength="80" bind:value={name} /></label>
      </div>
      <label class="field">
        <span>{t("users.role")}</span>
        <select bind:value={role}>{#each roleIds as r (r)}<option value={r}>{r}</option>{/each}</select>
      </label>
      <div class="row">
        <label class="check">
          <input type="checkbox" checked={scope === "*"} onchange={(e) => (scope = e.currentTarget.checked ? "*" : [])} />
          {t("users.all_projects")}
        </label>
        {#if scope !== "*"}
          {#each projects.list as project (project.id)}
            <label class="check">
              <input type="checkbox" checked={scope.includes(project.id)} onchange={() => (scope = toggleProject(scope, project.id))} />
              {project.name}
            </label>
          {/each}
        {/if}
      </div>
      <div><button class="btn primary" type="submit" disabled={busy === "create"}>{t("users.create")}</button></div>
    </form>
  {/if}
{/if}

{#if settings}
  <h2 class="section-h">{t("roles.title")}</h2>
  <div class="stack">
    {#each roleIds as r (r)}
      {#key `${r}:${(roles[r] ?? []).join(",")}`}
        <RoleEditor id={r} perms={roles[r] ?? []} all={catalog} onsave={saveRole(r)} />
      {/key}
    {/each}
    <form
      class="row"
      onsubmit={(e) => {
        e.preventDefault();
        if (newRole && !roles[newRole]) roles = { ...roles, [newRole]: [] };
      }}
    >
      <input type="text" placeholder={t("roles.new")} aria-label={t("roles.new")} pattern={"[a-z][a-z0-9_\\-]{0,31}"} bind:value={newRole} />
      <button class="btn small" type="submit">{t("action.create")}</button>
    </form>
  </div>
{/if}

<style>
  .back { color: var(--muted); text-decoration: none; font-size: 20px; }
  @media (min-width: 900px) { .back { display: none; } } /* «Назад» there */
  .people { list-style: none; margin: 0 0 8px; padding: 0; display: grid; gap: 8px; }
  .head { display: flex; gap: 10px; align-items: center; }
  .who { flex: 1; display: grid; min-width: 0; }
  .small { font-size: 12px; }
  .roles { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
  .editor { margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--line-soft); }
  .assign { display: flex; gap: 6px 12px; flex-wrap: wrap; align-items: center; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .grow { flex: 1 1 200px; }
  .check { display: flex; gap: 6px; align-items: center; font-size: 14px; }
  .add-role { max-width: 220px; }
  .issued { margin-bottom: 14px; }
  input[type="checkbox"] { accent-color: var(--accent); }
</style>
