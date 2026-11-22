// Who is signed in: /api/me (name, language, permissions per project, CSRF token, push key, factors).
// A 401 anywhere (the api's "kd:unauthorized") forgets the person; the app then shows the sign-in page.
import { ApiError, UNAUTHORIZED, apiGet, apiPost, setCsrf } from "./api";
import { clearDrafts } from "./drafts";
import { setLocale, setNames } from "./i18n.svelte";
import { unsubscribePush } from "./push";
import { navigate } from "./router.svelte";

export interface Me {
  id: string;
  name: string;
  language: string;
  timezone: string;
  permissions: Record<string, string[]>;
  projects: string[];
  csrf: string;
  push_public_key: string;
  second_factor: { passkey: boolean; totp: boolean; password: boolean };
  must_setup: boolean;
  /** People's audio is transcribed: a recording alone answers a text question. */
  voice?: boolean;
  /** The host runs Hindsight: its settings page and the projects' memory banks. */
  memory?: boolean;
  /** The host's card prefix: card IDs in texts are <cards>-<n>. */
  cards?: string;
  /** The host's command prefix: the catalogs' kd-<word> are <commands>-<word> here. */
  commands?: string;
  /** The host's earlier card prefixes: an old link /cards/<alias>-<n> opens <cards>-<n>. */
  card_aliases?: string[];
}

// the service worker's cache of API answers (sw.ts); a signed-out device keeps none of them — under this name or an
// earlier one (any "*-api" cache: the service worker also drops those when it activates).
export const API_CACHE = "kd-api";
const ALL = "*";

class Session {
  me = $state<Me | null>(null);
  loaded = $state(false);

  async load(): Promise<Me | null> {
    try {
      this.signedIn(await apiGet<Me>("/api/me"));
    } catch (exc) {
      if (!(exc instanceof ApiError) || exc.status !== 401) {
        this.loaded = true;
        throw exc;
      }
      this.clear();
    }
    this.loaded = true;
    return this.me;
  }

  signedIn(me: Me) {
    this.me = me;
    setCsrf(me.csrf);
    setNames(me.commands, me.cards, me.card_aliases);
    setLocale(me.language);
  }

  clear() {
    this.me = null;
    setCsrf("");
  }

  /** Sign out here, or `everywhere`: every session of the person, this one too. This device's push
   * subscription goes first (a signed-out device hears nothing); the plain sign-in page follows: a chosen
   * sign-out keeps no page to come back to. */
  async logout({ everywhere = false } = {}) {
    try {
      await unsubscribePush().catch(() => undefined);
      await apiPost(everywhere ? "/api/auth/logout-all" : "/api/auth/logout");
    } finally {
      navigate("/login", { replace: true });
      this.clear();
      clearDrafts(); // an unsent text does not outlive a chosen sign-out
      await dropCache();
    }
  }

  /** `perm` on `project`; without a project only a grant over every project counts (server: Users.can). */
  can(perm: string, project?: string): boolean {
    const table = this.me?.permissions ?? {};
    const scopes = [table[ALL] ?? []];
    if (project !== undefined && project !== ALL) scopes.push(table[project] ?? []);
    return scopes.some((granted) => granted.includes(ALL) || granted.includes(perm));
  }

  /** `perm` on at least one project (what a menu item or a button shows for). */
  canAny(perm: string): boolean {
    return Object.values(this.me?.permissions ?? {}).some(
      (granted) => granted.includes(ALL) || granted.includes(perm),
    );
  }
}

async function dropCache() {
  if (typeof caches === "undefined") return;
  for (const name of await caches.keys()) {
    if (name === API_CACHE || name.endsWith("-api")) await caches.delete(name);
  }
}

export const session = new Session();

if (typeof window !== "undefined") {
  addEventListener(UNAUTHORIZED, () => {
    session.clear();
    void dropCache();
  });
}
