import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clock, record, VOICE_MAX_MS, voiceName, voiceType, type RecorderEnv } from "./recorder";

class FakeRecorder {
  static supported = new Set(["audio/webm;codecs=opus", "audio/webm"]);
  static last: FakeRecorder;
  static isTypeSupported = (type: string) => FakeRecorder.supported.has(type);
  state: "inactive" | "recording" = "inactive";
  mimeType: string;
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(_stream: MediaStream, options?: MediaRecorderOptions) {
    this.mimeType = options?.mimeType ?? "audio/mp4";
    FakeRecorder.last = this;
  }
  start() {
    this.state = "recording";
  }
  stop() {
    this.state = "inactive";
    this.ondataavailable?.({ data: new Blob(["opus-bytes"], { type: this.mimeType }) });
    this.onstop?.();
  }
}

function env(now: () => number) {
  const track = { stop: vi.fn() };
  const stream = { getTracks: () => [track] } as unknown as MediaStream;
  const e: RecorderEnv = {
    getUserMedia: vi.fn(async () => stream),
    MediaRecorder: FakeRecorder as unknown as RecorderEnv["MediaRecorder"],
    now,
  };
  return { env: e, track };
}

describe("recorder", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("asks for webm/opus first and names the file by its time and type", () => {
    expect(voiceType((t) => t === "audio/mp4")).toBe("audio/mp4");
    expect(voiceType(() => true)).toBe("audio/webm;codecs=opus");
    expect(voiceType(() => false)).toBe("");
    const at = new Date(2026, 9, 20, 21, 30, 5);
    expect(voiceName("audio/webm;codecs=opus", at)).toBe("voice-20261020-213005.webm");
    expect(voiceName("audio/mp4", at)).toBe("voice-20261020-213005.m4a");
    expect(voiceName("audio/ogg", at)).toBe("voice-20261020-213005.ogg");
    expect(clock(0)).toBe("0:00");
    expect(clock(65_400)).toBe("1:05");
    expect(clock(VOICE_MAX_MS)).toBe("5:00");
  });

  it("gives the recorded file on stop and frees the microphone", async () => {
    let t = 0;
    const { env: e, track } = env(() => t);
    const done = vi.fn();
    const ticks: number[] = [];
    const rec = await record({ tick: (ms) => ticks.push(ms), done }, e);
    t = 1200;
    vi.advanceTimersByTime(250);
    expect(ticks.at(-1)).toBe(1200);
    rec.stop();
    const file = done.mock.calls[0][0] as File;
    expect(file.type).toBe("audio/webm");
    expect(file.name).toMatch(/^voice-\d{8}-\d{6}\.webm$/);
    expect(await file.text()).toBe("opus-bytes");
    expect(done.mock.calls[0][1]).toBe(false);
    expect(track.stop).toHaveBeenCalled();
    rec.stop(); // a second stop changes nothing
    expect(done).toHaveBeenCalledTimes(1);
  });

  it("stops itself at five minutes", async () => {
    let t = 0;
    const { env: e } = env(() => t);
    const done = vi.fn();
    await record({ tick: () => {}, done }, e);
    t = VOICE_MAX_MS - 1;
    vi.advanceTimersByTime(VOICE_MAX_MS - 1);
    expect(done).not.toHaveBeenCalled();
    t = VOICE_MAX_MS;
    vi.advanceTimersByTime(1);
    expect(done).toHaveBeenCalledTimes(1);
    expect(done.mock.calls[0][0]).toBeInstanceOf(File);
    expect(FakeRecorder.last.state).toBe("inactive");
  });

  it("a recorder's error stops it and marks the file broken", async () => {
    const { env: e } = env(() => 0);
    const done = vi.fn();
    await record({ tick: () => {}, done }, e);
    (FakeRecorder.last.onerror as () => void)();
    expect(done).toHaveBeenCalledTimes(1);
    expect(done.mock.calls[0][0]).toBeInstanceOf(File);
    expect(done.mock.calls[0][1]).toBe(true);
  });

  it("a cancelled recording gives nothing", async () => {
    const { env: e, track } = env(() => 0);
    const done = vi.fn();
    const rec = await record({ tick: () => {}, done }, e);
    rec.cancel();
    expect(done).toHaveBeenCalledWith(null, false);
    expect(track.stop).toHaveBeenCalled();
  });

  it("lets the browser choose when it supports none of the asked types (Safari: mp4)", async () => {
    FakeRecorder.supported = new Set();
    try {
      const { env: e } = env(() => 0);
      const done = vi.fn();
      const rec = await record({ tick: () => {}, done }, e);
      rec.stop();
      expect((done.mock.calls[0][0] as File).name).toMatch(/\.m4a$/);
      expect((done.mock.calls[0][0] as File).type).toBe("audio/mp4");
    } finally {
      FakeRecorder.supported = new Set(["audio/webm;codecs=opus", "audio/webm"]);
    }
  });

  it("a refused microphone rejects", async () => {
    const e: RecorderEnv = {
      getUserMedia: async () => {
        throw new DOMException("denied", "NotAllowedError");
      },
      MediaRecorder: FakeRecorder as unknown as RecorderEnv["MediaRecorder"],
      now: () => 0,
    };
    await expect(record({ tick: () => {}, done: () => {} }, e)).rejects.toThrow("denied");
  });
});
