// Voice messages: the microphone recorded in the format the browser writes (MediaRecorder: Chrome and
// Android webm/opus, Safari mp4) — the file goes as it is, nothing transcodes it. A recording is at most five
// minutes and stops itself there. No transcription yet: the voice message wakes nobody (a file without text).

export const VOICE_MAX_MS = 5 * 60 * 1000;
/** What a recording asks the browser for, in order; none supported — the browser's own choice. */
export const VOICE_TYPES = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
const TICK_MS = 250;
const SLICE_MS = 1000; // the recorder hands its data over every second: a stop loses nothing

export interface RecorderEnv {
  getUserMedia(constraints: MediaStreamConstraints): Promise<MediaStream>;
  MediaRecorder: { new (stream: MediaStream, options?: MediaRecorderOptions): MediaRecorder; isTypeSupported(type: string): boolean };
  now(): number;
}

export interface Recording {
  /** Stop and keep: `done` gets the file. */
  stop(): void;
  /** Stop and throw away: `done` gets null. */
  cancel(): void;
}

/** The browser can record here: a microphone API and MediaRecorder (a secure page, a recent browser). */
export function recordable(): boolean {
  return typeof MediaRecorder !== "undefined" && typeof navigator !== "undefined" && !!navigator.mediaDevices?.getUserMedia;
}

export function voiceType(supported: (type: string) => boolean): string {
  return VOICE_TYPES.find((type) => supported(type)) ?? "";
}

/** `voice-20261020-213005.webm`: the time it was recorded, the extension of its type. */
export function voiceName(mime: string, at: Date): string {
  const base = mime.split(";")[0].trim().toLowerCase();
  const ext = base === "audio/mp4" ? "m4a" : base === "audio/ogg" ? "ogg" : base === "audio/mpeg" ? "mp3" : base === "audio/wav" ? "wav" : "webm";
  const pad = (n: number) => String(n).padStart(2, "0");
  const day = `${at.getFullYear()}${pad(at.getMonth() + 1)}${pad(at.getDate())}`;
  return `voice-${day}-${pad(at.getHours())}${pad(at.getMinutes())}${pad(at.getSeconds())}.${ext}`;
}

/** `m:ss` of a duration. */
export function clock(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function browserEnv(): RecorderEnv {
  return {
    getUserMedia: (constraints) => navigator.mediaDevices.getUserMedia(constraints),
    MediaRecorder,
    now: () => Date.now(),
  };
}

/** Start recording: `tick` gets the time recorded, `done` the file once it stops (by `stop`, at five minutes, or
 * on the recorder's error — then `broken`) or null when cancelled or nothing was recorded. A refused microphone
 * rejects. */
export async function record(
  on: { tick(ms: number): void; done(file: File | null, broken: boolean): void },
  env: RecorderEnv = browserEnv(),
): Promise<Recording> {
  const stream = await env.getUserMedia({ audio: true });
  const release = () => stream.getTracks().forEach((track) => track.stop());
  const type = voiceType((t) => env.MediaRecorder.isTypeSupported(t));
  let recorder: MediaRecorder;
  try {
    recorder = new env.MediaRecorder(stream, type ? { mimeType: type } : undefined);
  } catch (exc) {
    release();
    throw exc;
  }
  const chunks: Blob[] = [];
  const started = env.now();
  let keep = true;
  let broken = false;
  let ended = false;
  const finish = () => {
    if (recorder.state !== "inactive") recorder.stop();
  };
  let ticker: ReturnType<typeof setInterval> | undefined;
  let limit: ReturnType<typeof setTimeout> | undefined;
  recorder.ondataavailable = (event) => {
    if (event.data && event.data.size) chunks.push(event.data);
  };
  recorder.onstop = () => {
    if (ended) return;
    ended = true;
    clearInterval(ticker);
    clearTimeout(limit);
    release();
    const mime = (recorder.mimeType || type || chunks[0]?.type || "audio/webm").split(";")[0].trim();
    on.done(keep && chunks.length ? new File(chunks, voiceName(mime, new Date(env.now())), { type: mime }) : null, broken);
  };
  recorder.onerror = () => {
    broken = true;
    finish();
  };
  try {
    recorder.start(SLICE_MS);
  } catch (exc) {
    ended = true;
    release();
    throw exc;
  }
  ticker = setInterval(() => on.tick(Math.min(env.now() - started, VOICE_MAX_MS)), TICK_MS);
  limit = setTimeout(finish, VOICE_MAX_MS);
  return {
    stop: finish,
    cancel: () => {
      keep = false;
      finish();
    },
  };
}
