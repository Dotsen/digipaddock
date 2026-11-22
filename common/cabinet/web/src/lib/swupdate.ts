// A rollout's first load is the old bundle: the old service worker serves it from its precache while
// the new worker installs and takes control (skipWaiting, clientsClaim). A page an older worker served
// reloads once onto the new bundle; drafts live in sessionStorage and survive it.
// an open tab never loads again (the app routes in the page), so the browser never asks for the new
// worker either — the page asks it itself when the tab comes back into view and every ten minutes.
type Workers = EventTarget & { controller: object | null; getRegistration?: () => Promise<{ update(): Promise<unknown> } | undefined> };
type Doc = EventTarget & { visibilityState: string };

export const UPDATE_EVERY_MS = 10 * 60_000;

export function reloadOnNewWorker(workers: Workers | undefined, reload: () => void): void {
  if (!workers?.controller) return;
  let reloaded = false;
  workers.addEventListener("controllerchange", () => {
    if (reloaded) return;
    reloaded = true;
    reload();
  });
}

/** Ask the server for a newer worker now and then; the reload above follows once it takes over. A failed check
 *  (offline, a restart) waits for the next one. */
export function checkForNewWorker(workers: Workers | undefined, doc: Doc, every = UPDATE_EVERY_MS): () => void {
  if (!workers?.getRegistration) return () => {};
  const check = () => {
    void workers.getRegistration!().then((r) => r?.update()).catch(() => undefined);
  };
  const onVisible = () => {
    if (doc.visibilityState === "visible") check();
  };
  doc.addEventListener("visibilitychange", onVisible);
  const timer = setInterval(check, every);
  return () => {
    doc.removeEventListener("visibilitychange", onVisible);
    clearInterval(timer);
  };
}
