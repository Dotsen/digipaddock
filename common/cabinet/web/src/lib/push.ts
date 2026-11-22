// Web Push on this device: the browser's subscription, sent to the cabinet with a device name.
import { apiPost } from "./api";
import { fromBase64url } from "./webauthn";

import { needsInstall, type PushLast, type PushState } from "./pushview";

export type { PushLast, PushState } from "./pushview";

export function pushSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

/** "on" only when the browser's subscription is registered for the person signed in: the device may
 * still hold another person's (global review 15.10.2026). "lost" — notifications were allowed here
 * but the subscription is gone; "install" — this device takes push only as an installed app (iOS); `last` — this
 * device's last delivery as the gateway answered. */
export async function pushDetails(): Promise<{ state: PushState; last: PushLast | null }> {
  if (!pushSupported()) {
    const standalone = typeof matchMedia === "function" && matchMedia("(display-mode: standalone)").matches;
    return { state: needsInstall(navigator.userAgent, standalone) ? "install" : "unsupported", last: null };
  }
  if (Notification.permission === "denied") return { state: "denied", last: null };
  const allowed = Notification.permission === "granted";
  const registration = await navigator.serviceWorker.getRegistration();
  const subscription = await registration?.pushManager.getSubscription();
  if (!subscription) return { state: allowed ? "lost" : "off", last: null };
  try {
    const found = await apiPost<{ mine: boolean; last?: PushLast | null }>("/api/push/state", { endpoint: subscription.endpoint });
    return found.mine ? { state: "on", last: found.last ?? null } : { state: allowed ? "lost" : "off", last: null };
  } catch {
    return { state: "off", last: null };
  }
}

export async function pushState(): Promise<PushState> {
  return (await pushDetails()).state;
}

/** "Chrome · Windows" from a user agent (this browser's by default). */
export function deviceName(agent: string = navigator.userAgent): string {
  const os = /iPhone|iPad/.test(agent)
    ? "iOS"
    : /Android/.test(agent)
      ? "Android"
      : /Mac OS X/.test(agent)
        ? "macOS"
        : /Windows/.test(agent)
          ? "Windows"
          : "Linux";
  const browser = /Edg\//.test(agent)
    ? "Edge"
    : /Firefox\//.test(agent)
      ? "Firefox"
      : /Chrome\//.test(agent)
        ? "Chrome"
        : /Safari\//.test(agent)
          ? "Safari"
          : "browser";
  return `${browser} · ${os}`;
}

/** Ask for permission, subscribe with the cabinet's VAPID key and register the subscription. */
export async function subscribePush(publicKey: string): Promise<PushState> {
  if (!pushSupported()) return "unsupported";
  const permission = await Notification.requestPermission();
  if (permission !== "granted") return permission === "denied" ? "denied" : "off";
  const registration = await navigator.serviceWorker.ready;
  // a subscription of someone who signed in on this device before is dropped, not re-registered
  const held = await registration.pushManager.getSubscription();
  if (held) await held.unsubscribe();
  const subscription = await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: fromBase64url(publicKey),
  });
  await apiPost("/api/push/subscribe", { subscription: subscription.toJSON(), device: deviceName() });
  return "on";
}

/** Stop notifications on this device: the cabinet forgets the subscription, the browser drops it. */
export async function unsubscribePush(): Promise<PushState> {
  if (!pushSupported()) return "unsupported";
  const registration = await navigator.serviceWorker.getRegistration();
  const subscription = await registration?.pushManager.getSubscription();
  if (subscription) {
    await apiPost("/api/push/unsubscribe", { endpoint: subscription.endpoint });
    await subscription.unsubscribe();
  }
  return "off";
}
