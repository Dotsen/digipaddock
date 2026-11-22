import { beforeEach, describe, expect, it } from "vitest";
import { ApiError } from "./api";
import { FILE_MAX, FILES_PER_MESSAGE, MESSAGE_FILES_MAX, filesOf, isAudio, isPreview, problem, refusal, retryRefusal, textOf, voiceView } from "./attachments";
import { setLocale } from "./i18n.svelte";

beforeEach(() => setLocale("ru"));

describe("attachments", () => {
  it("says what stands under an audio (KD-9076)", () => {
    const audio = (voice?: object, deleted = false) => ({ id: "x", name: "v.webm", mime: "audio/webm", voice, deleted } as any);
    expect(voiceView(audio())).toBeNull();
    expect(voiceView(audio({ state: "queued", position: 3 }))).toMatchObject({ line: "в очереди: 3", retry: false });
    expect(voiceView(audio({ state: "running" }))).toMatchObject({ line: "расшифровывается…", failed: false });
    expect(voiceView(audio({ state: "done", status: "ok", text: "привет" }))).toMatchObject({ text: "привет", line: "" });
    expect(voiceView(audio({ state: "done", status: "unavailable" }))).toMatchObject({ failed: true, retry: true });
    const empty = voiceView(audio({ state: "done", status: "empty" }));
    expect(empty).toMatchObject({ failed: true, retry: false });
    expect(empty?.line).toBe("Аудио не расшифровано: речь не распознана — запишите заново");
    expect(voiceView(audio({ state: "done", status: "too_long" }))?.retry).toBe(false);
    // the switch closed the line — said so, and "Retry" once it is on again
    expect(voiceView(audio({ state: "done", status: "off" }))).toMatchObject({
      line: "Аудио не расшифровано: расшифровка выключена", failed: true, retry: true });
    expect(voiceView(audio({ state: "done", status: "ok", text: "x" }, true))).toBeNull();
  });

  it("reads both payload shapes", () => {
    const old = { kind: "file", body: "a.txt", payload: { id: "a".repeat(32), name: "a.txt", mime: "text/plain", size: 3 } };
    expect(filesOf(old)).toEqual([old.payload]);
    expect(textOf(old)).toBe("");
    const files = [{ id: "b".repeat(32), name: "b.png", mime: "image/png", size: 9 }, { id: "c".repeat(32), name: "c", deleted: true }];
    const both = { kind: "text", body: "смотри", payload: { files } };
    expect(filesOf(both)).toEqual(files);
    expect(textOf(both)).toBe("смотри");
    expect(filesOf({ kind: "text", payload: {} })).toEqual([]);
  });

  it("previews the four raster types only, and never a deleted file", () => {
    expect(["image/png", "image/jpeg", "image/gif", "image/webp"].every((mime) => isPreview({ id: "x", name: "x", mime }))).toBe(true);
    expect(isPreview({ id: "x", name: "x.svg", mime: "image/svg+xml" })).toBe(false);
    expect(isPreview({ id: "x", name: "x.png", mime: "image/png", deleted: true })).toBe(false);
  });

  it("plays audio in the thread, never a deleted one (KD-9077)", () => {
    expect(["audio/webm", "audio/mp4", "audio/ogg"].every((mime) => isAudio({ id: "x", name: "v", mime }))).toBe(true);
    expect(isAudio({ id: "x", name: "v.webm", mime: "video/webm" })).toBe(false);
    expect(isAudio({ id: "x", name: "v.webm", mime: "audio/webm", deleted: true })).toBe(false);
    expect(isPreview({ id: "x", name: "v.webm", mime: "audio/webm" })).toBe(false);
  });

  it("says why files cannot go", () => {
    expect(problem([{ name: "a", size: FILE_MAX }])).toBe("");
    expect(problem([{ name: "big.mov", size: FILE_MAX + 1 }])).toContain("big.mov");
    const five = Array.from({ length: 5 }, (_, i) => ({ name: `${i}`, size: FILE_MAX }));
    expect(problem(five)).toBe("");
    expect(problem([...five, { name: "x", size: 1 }])).not.toBe("");
    expect(problem(Array.from({ length: FILES_PER_MESSAGE + 1 }, () => ({ name: "a", size: 1 })))).toContain(String(FILES_PER_MESSAGE));
    expect(MESSAGE_FILES_MAX).toBe(100 * 1024 * 1024);
  });

  it("says a retry waits for the switch (KD-9184)", () => {
    expect(retryRefusal(new ApiError(403, "refused", "x", { reason: "voice_off" })))
      .toBe("Расшифровка выключена в кабинете — «Повторить» сработает, когда её включат");
    expect(retryRefusal(new ApiError(403, "refused", "x"))).toBe(refusal(new ApiError(403, "refused", "x")));
  });

  it("names a quota refusal by its reason", () => {
    const quota = new ApiError(403, "refused", "your files…", { reason: "files_quota" });
    expect(refusal(quota)).not.toBe(refusal(new ApiError(403, "refused", "x")));
    expect(refusal(new ApiError(413, "too_large", "x"))).toContain("100");
  });
});
