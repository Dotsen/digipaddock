// A history router on $state: one current route, path parameters, the query string.
// Same-origin <a href> clicks become navigate() unless they point at /api/ (downloads) or carry modifiers.

export type RouteName =
  | "login"
  | "setup"
  | "dashboard"
  | "inbox"
  | "board"
  | "card"
  | "decision"
  | "agents"
  | "agent"
  | "feed"
  | "settings"
  | "users"
  | "projects"
  | "models"
  | "hindsight"
  | "servers"
  | "server"
  | "monitor-settings"
  | "incident"
  | "incidents"
  | "deploys"
  | "notfound";

export interface Route {
  name: RouteName;
  params: Record<string, string>;
  query: URLSearchParams;
  path: string;
}

const TABLE: [RouteName, RegExp, string[]][] = [
  ["dashboard", /^\/$/, []],
  ["inbox", /^\/inbox$/, []],
  ["login", /^\/login$/, []],
  ["setup", /^\/setup$/, []],
  ["board", /^\/board$/, []],
  ["card", /^\/cards\/([^/]+)$/, ["id"]],
  // the old BOSS page's links (nginx: /boss/promotions/<id> -> /decisions/by-proposal/<id>)
  ["decision", /^\/decisions\/by-proposal\/([0-9a-f]{32})$/, ["proposal"]],
  ["decision", /^\/decisions\/([^/]+)$/, ["id"]],
  ["agents", /^\/agents$/, []],
  ["agent", /^\/agents\/([^/]+)$/, ["name"]],
  ["feed", /^\/feed$/, []],
  ["settings", /^\/settings$/, []],
  ["users", /^\/settings\/users$/, []],
  ["projects", /^\/settings\/projects$/, []],
  ["models", /^\/settings\/models$/, []], // the models of other providers
  ["hindsight", /^\/settings\/hindsight$/, []],
  // the monitor's servers and incidents P-n
  ["servers", /^\/servers$/, []],
  // before the server's own page — «settings» is no server's alias there
  ["monitor-settings", /^\/servers\/settings$/, []],
  ["server", /^\/servers\/([^/]+)$/, ["alias"]],
  ["incident", /^\/incidents\/(P-[0-9]+)$/, ["id"]],
  ["incidents", /^\/incidents$/, []],
  // the projects' deploys and their journal
  ["deploys", /^\/deploys$/, []],
];

function decode(part: string): string {
  try {
    return decodeURIComponent(part);
  } catch {
    return part; // "%E0%A4%A" and the like: the screen says "not found", the page does not crash
  }
}

export function match(full: string): Route {
  const url = new URL(full, "http://cabinet.invalid");
  const pathname = url.pathname.length > 1 ? url.pathname.replace(/\/+$/, "") : url.pathname;
  const path = url.pathname + url.search;
  for (const [name, pattern, keys] of TABLE) {
    const found = pattern.exec(pathname);
    if (!found) continue;
    const params: Record<string, string> = {};
    keys.forEach((key, i) => (params[key] = decode(found[i + 1])));
    return { name, params, query: url.searchParams, path };
  }
  return { name: "notfound", params: {}, query: url.searchParams, path };
}

function here(): string {
  return location.pathname + location.search;
}

// how many steps inside the cabinet this tab's entry is from where the tab came in. It rides in the
// history entry's state, so a reload or the browser's own back keeps it; an entry the cabinet did not push (a
// tab opened on a link) has none — depth 0, and "Back" goes to the page's parent instead of leaving.
const depthOf = (state: unknown): number => {
  const depth = (state as { kdDepth?: unknown } | null)?.kdDepth;
  return typeof depth === "number" && depth > 0 ? depth : 0;
};

class Router {
  current = $state<Route>(match(typeof location === "undefined" ? "/" : here()));
  depth = $state(typeof history === "undefined" ? 0 : depthOf(history.state));
}

export const router = new Router();

export function navigate(path: string, { replace = false } = {}) {
  if (path !== here()) {
    if (replace) history.replaceState({ kdDepth: router.depth }, "", path);
    else {
      router.depth += 1;
      history.pushState({ kdDepth: router.depth }, "", path);
    }
  }
  router.current = match(path);
}

/** The page's parent for "Back", or null on a menu section (it has no "Back"). A card opened on a
 * decision (/decisions/<id> lands there with ?decision=) goes back to the inbox. */
export function parentOf(route: Route): string | null {
  switch (route.name) {
    case "card":
      return route.query.has("decision") ? "/inbox" : "/board";
    case "decision":
      return "/inbox";
    case "agent":
      return "/agents";
    case "users":
    case "projects":
    case "models":
    case "hindsight":
      return "/settings";
    case "server":
    case "monitor-settings":
      return "/servers";
    case "incident":
      return "/incidents";
    default:
      return null;
  }
}

/** "Back": the previous page of this tab when the tab got here inside the cabinet, its parent otherwise —
 * never out of the cabinet. */
export function goBack() {
  if (router.depth > 0) {
    history.back();
    return;
  }
  const parent = parentOf(router.current);
  navigate(parent ?? "/", { replace: true });
}

export function handleLinkClick(event: MouseEvent): boolean {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
    return false;
  }
  const target = event.target instanceof Element ? event.target.closest("a") : null;
  if (!target || target.target || target.hasAttribute("download")) return false;
  const url = new URL(target.href, location.href);
  if (url.origin !== location.origin || url.pathname.startsWith("/api/")) return false;
  // an anchor on the page shown is the browser's: it scrolls there and the route stays
  if (url.hash && url.pathname === location.pathname && url.search === location.search) return false;
  event.preventDefault();
  navigate(url.pathname + url.search);
  return true;
}

let started = false;

export function startRouter() {
  router.current = match(here());
  router.depth = depthOf(history.state);
  if (started) return;
  started = true;
  addEventListener("popstate", () => {
    router.current = match(here());
    router.depth = depthOf(history.state);
  });
  document.addEventListener("click", handleLinkClick);
}
