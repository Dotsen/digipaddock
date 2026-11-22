// "12 min", "3 h", "2 d" — how long ago, or how long is left.
import { getLocale, t } from "./i18n.svelte";

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

function span(ms: number): string {
  if (ms < HOUR) return t("time.minutes", { n: Math.max(0, Math.floor(ms / MINUTE)) });
  if (ms < DAY) return t("time.hours", { n: Math.floor(ms / HOUR) });
  return t("time.days", { n: Math.floor(ms / DAY) });
}

export function age(iso: string, now = Date.now()): string {
  return iso ? span(Math.max(0, now - Date.parse(iso))) : "";
}

export function remaining(iso: string, now = Date.now()): string {
  if (!iso) return "";
  const left = Date.parse(iso) - now;
  return left > 0 ? span(left) : "";
}

/** "14:05" — minutes and seconds left until `iso`, or "" once it has passed (a sign-in code). */
export function countdown(iso: string, now = Date.now()): string {
  const left = iso ? Math.ceil((Date.parse(iso) - now) / 1000) : 0;
  if (!(left > 0)) return "";
  return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
}

export function when(iso: string): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** "08.11 15:00" in the person's time zone (the moment a card is put off until); the device's own zone when
 *  the person's is unknown to the browser. */
export function dayTime(iso: string, zone = ""): string {
  if (!iso) return "";
  const options: Intl.DateTimeFormatOptions = { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" };
  let format: Intl.DateTimeFormat;
  try {
    format = new Intl.DateTimeFormat("ru-RU", { ...options, timeZone: zone || undefined });
  } catch {
    format = new Intl.DateTimeFormat("ru-RU", options);
  }
  const parts = format.formatToParts(new Date(iso));
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((p) => p.type === type)?.value ?? "";
  return `${part("day")}.${part("month")} ${part("hour")}:${part("minute")}`;
}

/** "09:05": the time of day of a Date.now() moment. */
export function clock(ms: number): string {
  return new Date(ms).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", hour12: false });
}

const TAGS = { ru: "ru-RU", en: "en-GB" } as const;

/** An incident's factual clock: date, year, 24-hour time and the viewer's timezone. */
export function exactWhen(iso: string): string {
  return iso ? new Date(iso).toLocaleString(TAGS[getLocale()], {
    day:"2-digit", month:"2-digit", year:"numeric", hour:"2-digit", minute:"2-digit", hour12:false,
    timeZoneName:"short",
  }) : "";
}

/** A message's time: "09:07" today, "yesterday, 23:59", "3 Oct, 14:32" this year, "31.12.2025, 08:00". */
export function stamp(iso: string, now = Date.now()): string {
  if (!iso) return "";
  const at = new Date(iso);
  const tag = TAGS[getLocale()];
  const time = at.toLocaleTimeString(tag, { hour: "2-digit", minute: "2-digit", hour12: false });
  const today = new Date(now);
  const day = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((day(today) - day(at)) / DAY);
  if (days === 0) return time;
  if (days === 1) return t("time.yesterday", { time });
  const date =
    at.getFullYear() === today.getFullYear()
      ? at.toLocaleDateString(tag, { day: "numeric", month: "short" })
      : at.toLocaleDateString(tag, { day: "2-digit", month: "2-digit", year: "numeric" });
  return `${date}, ${time}`;
}
