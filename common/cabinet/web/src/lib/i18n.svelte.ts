// The UI's two catalogs (messages/ru.json, messages/en.json — the same files the server renders from).
// `Key` is the catalog's key set, so svelte-check fails on a key that does not exist.
import en from "../../messages/en.json";
import ru from "../../messages/ru.json";
import { ApiError } from "./api";
import { CANON, canonicalCard, rewriteNames, type Names } from "./names";

export type Locale = "ru" | "en";
export type Key = Exclude<keyof typeof ru, "$schema">;

const CATALOGS: Record<Locale, Record<string, string>> = { ru, en };
// a server kind that has a catalog word under another name
const KIND_ALIASES: Record<string, string> = { refused: "forbidden" };

export function detectLocale(language: string | undefined): Locale {
  return language?.toLowerCase().startsWith("ru") ? "ru" : "en";
}

class I18n {
  locale = $state<Locale>(detectLocale(typeof navigator === "undefined" ? "" : navigator.language));
  names = $state<Names>({ ...CANON });
}

const state = new I18n();

export function setLocale(locale: string) {
  state.locale = locale === "ru" || locale === "en" ? locale : detectLocale(locale);
  if (typeof document !== "undefined") document.documentElement.lang = state.locale;
}

export function getLocale(): Locale {
  return state.locale;
}

/** The host's command and card prefixes and its earlier card prefixes (`/api/me`); none — the repository's. */
export function setNames(commands?: string, cards?: string, aliases?: string[]) {
  state.names = { commands: commands || CANON.commands, cards: cards || CANON.cards, aliases: aliases ?? [] };
}

/** A card ID as the host names it now: an old link's earlier prefix becomes the current one. */
export function currentCard(id: string): string {
  return canonicalCard(id, state.names);
}

export function getNames(): Names {
  return state.names;
}

export function t(key: Key, params: Record<string, string | number> = {}): string {
  const text = rewriteNames(CATALOGS[state.locale][key] ?? ru[key] ?? key, state.names);
  return text.replace(/\{(\w+)\}/g, (whole, name) =>
    name in params ? String(params[name]) : whole,
  );
}

export function hasKey(key: string): key is Key {
  return key !== "$schema" && key in ru;
}

export function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    // the server's reason first: «the card changed» must not stand for BOSS being off or an expired
    // proposal
    const reason = typeof error.body.reason === "string" ? `error.reason.${error.body.reason}` : "";
    if (reason && hasKey(reason)) return t(reason);
    const kind = KIND_ALIASES[error.kind] ?? error.kind;
    const key = `error.${kind}`;
    if (hasKey(key)) return t(key);
  }
  return t("error.unknown");
}
