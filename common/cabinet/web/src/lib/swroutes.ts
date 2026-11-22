// What the service worker treats as the app's own navigation (served from the precached app shell) and
// what it leaves to the server: the API and the live channel, the old monitor addresses (nginx answers them 404
// since the change), the legacy links nginx redirects into the cabinet, the well-known files and the diagram frame
// (a page of its own under its own policy). Plain code: sw.ts and the tests import it. (Global review
// 15.10.2026: the shell swallowed /monitor/ and every legacy link.)

export const NOT_THE_APP = [
  /^\/api\//,
  /^\/ws$/,
  /^\/monitor(\/|$)/,
  /^\/board\//,
  /^\/admin\//,
  /^\/boss\//,
  /^\/\.well-known\//,
  /^\/diagram\.html$/,
];

export function isAppNavigation(pathname: string): boolean {
  return !NOT_THE_APP.some((pattern) => pattern.test(pathname));
}

/** A link a notification may open: a path of this origin — never `//host` (a protocol-relative URL). */
export function safeAppPath(url: unknown, fallback = "/inbox"): string {
  if (typeof url !== "string" || !url.startsWith("/") || url.startsWith("//") || /[\\\u0000-\u001f\u007f]/.test(url)) return fallback;
  const parsed = new URL(url, "https://cabinet.invalid");
  return parsed.origin === "https://cabinet.invalid" ? parsed.pathname + parsed.search + parsed.hash : fallback;
}
