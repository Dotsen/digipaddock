// The inbox's count outside the menu: the tab's title «DIGIPADDOCK (N)», a dot on the favicon and,
// in an installed app that supports the Badging API, the icon's badge. Without the API the title and favicon still work.
import { titleFor } from "./unread";

const ICON = "/icons/icon.svg";
const ICON_UNREAD = "/icons/icon-unread.svg";

type BadgingNavigator = Navigator & {
  setAppBadge?: (count?: number) => Promise<void>;
  clearAppBadge?: () => Promise<void>;
};

export function showUnread(base: string, unread: number) {
  if (typeof document === "undefined") return;
  document.title = titleFor(base, unread);
  const icon = document.querySelector<HTMLLinkElement>('link[rel="icon"]');
  const href = unread > 0 ? ICON_UNREAD : ICON;
  if (icon && icon.getAttribute("href") !== href) icon.setAttribute("href", href);
  const nav = (typeof navigator === "undefined" ? undefined : navigator) as BadgingNavigator | undefined;
  try {
    if (unread > 0) void nav?.setAppBadge?.(unread)?.catch(() => {});
    else void nav?.clearAppBadge?.()?.catch(() => {});
  } catch {
    // no Badging API here: the menu, the title and the favicon carry it
  }
}
