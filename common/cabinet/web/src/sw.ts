/// <reference lib="webworker" />
// The service worker: the app shell from the precache, API reads network-first (the last answer when
// offline), Web Push notifications and their clicks (open the link, or focus the app and send it there).
import { clientsClaim } from "workbox-core";
import { cleanupOutdatedCaches, createHandlerBoundToURL, precacheAndRoute } from "workbox-precaching";
import { NavigationRoute, registerRoute } from "workbox-routing";
import { NetworkFirst } from "workbox-strategies";
import { notificationOf, type PushPayload } from "./lib/swnotify";
import { NOT_THE_APP } from "./lib/swroutes";
import { openNotification } from "./lib/swclick";

declare const self: ServiceWorkerGlobalScope & { __WB_MANIFEST: Array<{ url: string; revision: string | null }> };

// session.svelte.ts drops this cache on sign-out; a cache of API answers under an earlier name goes on activation
const API_CACHE = "kd-api";
const EARLIER_API_CACHE = /-api$/;
const NOT_CACHED = /^\/api\/(auth|files|stepup|push)\//;

self.skipWaiting();
clientsClaim();
cleanupOutdatedCaches();
self.addEventListener("activate", (event: ExtendableEvent) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== API_CACHE && EARLIER_API_CACHE.test(n)).map((n) => caches.delete(n))),
    ),
  );
});
precacheAndRoute(self.__WB_MANIFEST);

// the app shell for the app's own screens; the API, the live channel, the monitor pages and the legacy
// links nginx redirects go to the server (lib/swroutes.ts)
registerRoute(new NavigationRoute(createHandlerBoundToURL("/index.html"), { denylist: NOT_THE_APP }));

registerRoute(
  ({ url, request }) =>
    url.origin === self.location.origin &&
    request.method === "GET" &&
    url.pathname.startsWith("/api/") &&
    !NOT_CACHED.test(url.pathname),
  new NetworkFirst({ cacheName: API_CACHE, networkTimeoutSeconds: 5 }),
);

self.addEventListener("push", (event: PushEvent) => {
  let data: PushPayload = {};
  try {
    data = event.data?.json() ?? {};
  } catch {
    data = { body: event.data?.text() };
  }
  // every push carries the person's inbox count — the installed app's badge follows it where the
  // Badging API exists (a visible push only: no silent pushes just for a count)
  const badge = typeof data.badge === "number" ? data.badge : null;
  const nav = self.navigator as WorkerNavigator & { setAppBadge?: (n?: number) => Promise<void> };
  const { title, options } = notificationOf(data);
  event.waitUntil(
    Promise.all([
      self.registration.showNotification(title, options),
      badge !== null && nav.setAppBadge ? nav.setAppBadge(badge).catch(() => undefined) : Promise.resolve(),
    ]),
  );
});

self.addEventListener("notificationclick", (event: NotificationEvent) => {
  event.notification.close();
  event.waitUntil(openNotification(self.clients, self.location.origin, event.notification.data?.url));
});
