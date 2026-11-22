// What the person is told about push on this device: its state and the last delivery attempt, in words.

/** on — subscribed for this person; off — not yet; lost — allowed but the subscription is gone; denied — blocked by
 * the browser; install — this device takes push only as an installed app; unsupported — no push here at all. */
export type PushState = "unsupported" | "install" | "denied" | "off" | "lost" | "on";

/** This device's last delivery as the push gateway answered: accepted is not proof the system showed it. */
export interface PushLast {
  result: string;
  at: string;
  sent: boolean;
}

/** iOS and iPadOS take Web Push only in an app added to the home screen. */
export function needsInstall(agent: string, standalone: boolean): boolean {
  return /iPhone|iPad|iPod/.test(agent) && !standalone;
}

/** The chat's banner: shown until this device is connected, and not again in a session after "Later". */
export function bannerShown(state: PushState, later: boolean): boolean {
  return state !== "on" && !later;
}

export type BannerKey =
  | "push.banner.off"
  | "push.banner.lost"
  | "push.banner.denied"
  | "push.banner.install"
  | "push.banner.unsupported";

/** The banner's line for a device that is not on (the catalog has no «on» line: the banner is gone then). */
export function bannerKey(state: Exclude<PushState, "on">): BannerKey {
  return `push.banner.${state}`;
}

export type LastKey = "push.last.none" | "push.last.accepted" | "push.last.failed";

/** The settings' line about the last attempt: accepted by the gateway, failed with its code, or none yet. */
export function lastLine(last: PushLast | null): { key: LastKey; code: string; at: string } {
  if (!last) return { key: "push.last.none", code: "", at: "" };
  if (last.sent) return { key: "push.last.accepted", code: last.result, at: last.at };
  return { key: "push.last.failed", code: last.result === "0" ? "—" : last.result, at: last.at };
}
