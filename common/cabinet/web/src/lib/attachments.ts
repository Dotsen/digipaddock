// A message's files: read from either payload shape, checked against the cabinet's limits before a send,
// and the refusal of a send in words. The server holds the same limits (api.FILE_MAX, MESSAGE_FILES_MAX,
// FILES_PER_MESSAGE) and refuses the whole message; the page only says why earlier.
import { ApiError } from "./api";
import { bytes } from "./bytes";
import { errorText, hasKey, t, type Key } from "./i18n.svelte";
import type { Message } from "./types";

export const FILE_MAX = 20 * 1024 * 1024;
export const MESSAGE_FILES_MAX = 100 * 1024 * 1024;
export const FILES_PER_MESSAGE = 20;
/** Shown as pictures in the thread; SVG and the rest are downloads. */
export const PREVIEW_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);
/** Played in the thread: what the server serves inline as audio (files.AUDIO_TYPES). */
export const AUDIO_TYPES = new Set(["audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/wav", "audio/aac", "audio/x-m4a"]);

/** An audio's transcription: in the line (its place), being transcribed, or its result. */
export interface Voice {
  state: "queued" | "running" | "done";
  status?: string;
  text?: string;
  position?: number;
}

export interface Attachment {
  id: string;
  name: string;
  mime?: string;
  size?: number;
  deleted?: boolean;
  voice?: Voice;
}

/** A failure of the way to the transcriber, or the switch that closed the line: "Retry" puts the
 * audio back in the line (voice.RETRY). */
export const VOICE_RETRY = new Set(["error", "unavailable", "bad_request", "off"]);

/** What stands under an audio: its place in the line, «transcribing», its text, or why there is none and whether
 * a retry may help; null — not transcribed at all (an agent's file, one from before, the queue off). */
export function voiceView(file: Attachment): { line: string; text: string; failed: boolean; retry: boolean } | null {
  const v = file.voice;
  if (!v || file.deleted) return null;
  if (v.state === "queued") return { line: t("voice.queued", { n: v.position ?? 1 }), text: "", failed: false, retry: false };
  if (v.state !== "done") return { line: t("voice.running"), text: "", failed: false, retry: false };
  if (v.status === "ok") return { line: "", text: v.text ?? "", failed: false, retry: false };
  const status = v.status ?? "error";
  const why = hasKey(`voice.status.${status}`) ? t(`voice.status.${status}` as Key) : status;
  return { line: `${t("voice.not_transcribed")}: ${why}`, text: "", failed: true, retry: VOICE_RETRY.has(status) };
}

/** The message's files: `payload.files`, or the one file of an older `file` message. */
export function filesOf(message: Pick<Message, "kind" | "payload">): Attachment[] {
  const payload = message.payload ?? {};
  if (Array.isArray(payload.files)) return payload.files.filter((f: unknown) => !!f && typeof (f as Attachment).id === "string");
  if (message.kind === "file" && typeof payload.id === "string") return [payload as Attachment];
  return [];
}

/** The message's own text: a `file` message's body only names its files. */
export function textOf(message: Pick<Message, "kind" | "body">): string {
  return message.kind === "file" ? "" : message.body;
}

export const isPreview = (file: Attachment): boolean => !file.deleted && PREVIEW_TYPES.has(file.mime ?? "");
export const isAudio = (file: Attachment): boolean => !file.deleted && AUDIO_TYPES.has(file.mime ?? "");

/** Why these files cannot go in one message, or "" when they can. */
export function problem(files: Pick<File, "name" | "size">[]): string {
  if (files.length > FILES_PER_MESSAGE) return t("files.too_many", { max: FILES_PER_MESSAGE });
  const big = files.find((f) => f.size > FILE_MAX);
  if (big) return t("files.too_big", { name: big.name, max: bytes(FILE_MAX) });
  const total = files.reduce((sum, f) => sum + f.size, 0);
  if (total > MESSAGE_FILES_MAX) return t("files.too_much", { max: bytes(MESSAGE_FILES_MAX) });
  return "";
}

/** A refused send in words: a quota by its reason, an over-limit body, else the error's kind. */
export function refusal(exc: unknown): string {
  if (exc instanceof ApiError) {
    const reason = exc.body?.reason;
    if (reason === "files_quota") return t("files.files_quota");
    if (reason === "files_full") return t("files.files_full");
    if (exc.status === 413) return t("files.too_much", { max: bytes(MESSAGE_FILES_MAX) });
  }
  return errorText(exc);
}

/** A refused "Retry" in words: the switch that is off says so, else the error's kind. */
export function retryRefusal(exc: unknown): string {
  if (exc instanceof ApiError && exc.body?.reason === "voice_off") return t("voice.retry_off");
  return errorText(exc);
}

/** The files a paste or a drop carries (a screenshot from the clipboard has no name of its own). */
export function carried(data: DataTransfer | null): File[] {
  if (!data) return [];
  const out = Array.from(data.files ?? []);
  return out.map((file, i) =>
    file.name && file.name !== "image.png"
      ? file
      : new File([file], `screenshot-${stampName()}${out.length > 1 ? `-${i + 1}` : ""}.${extension(file.type)}`, {
          type: file.type,
        }),
  );
}

function extension(mime: string): string {
  const tail = mime.split("/")[1] ?? "";
  return /^[a-z0-9]{1,5}$/.test(tail) ? (tail === "jpeg" ? "jpg" : tail) : "bin";
}

function stampName(now = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
}
