// The notification a push shows (sw.ts). Plain code: the service worker and the tests import it.
import { safeAppPath } from "./swroutes";

export interface PushPayload {
  title?: string;
  body?: string;
  tag?: string;
  url?: string;
  /** the person's inbox count as the push went */
  badge?: number;
}

/** `renotify` is in the Notifications API but not in every TypeScript lib. */
export type ShownOptions = NotificationOptions & { renotify?: boolean };

/** The title and options of one push's notification. a card's events, a source's notices and an agent's
 * batched replies share a tag, so a new one replaces the one still in the shade — without `renotify` Android does
 * that silently (no sound, no heads-up), and the push looks lost. */
export function notificationOf(data: PushPayload): { title: string; options: ShownOptions } {
  const options: ShownOptions = {
    body: data.body ?? "",
    // the manifest's icons carry a version in their names — a new mark under the old name leaves an
    // installed
    // app with its old icon (Chrome updates it on a changed manifest only)
    icon: "/icons/icon-192.v2.png",
    // The status bar draws only the badge's alpha: the brand mark on transparency.
    badge: "/icons/badge-96.png",
    data: { url: safeAppPath(data.url) },
  };
  if (data.tag) {
    options.tag = data.tag;
    options.renotify = true;
  }
  return { title: data.title || "DIGIPADDOCK", options };
}
