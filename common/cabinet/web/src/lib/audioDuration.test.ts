import { describe, expect, it } from "vitest";
import { fullDuration } from "./audioDuration";

class FakeAudio extends EventTarget {
  duration = Number.POSITIVE_INFINITY;
  seeks: number[] = [];
  private time = 0;
  get currentTime() {
    return this.time;
  }
  set currentTime(value: number) {
    this.time = value;
    this.seeks.push(value);
  }
  fire(kind: string) {
    this.dispatchEvent(new Event(kind));
  }
}

describe("fullDuration (KD-9076)", () => {
  it("finds the length of a webm without one and goes back to the start", () => {
    const audio = new FakeAudio();
    fullDuration(audio as unknown as HTMLMediaElement);
    audio.fire("loadedmetadata");
    expect(audio.seeks).toEqual([1e101]);
    audio.fire("timeupdate"); // still unknown: wait
    expect(audio.seeks).toEqual([1e101]);
    audio.duration = 17.3;
    audio.fire("durationchange");
    expect(audio.seeks).toEqual([1e101, 0]);
    audio.fire("timeupdate"); // once only
    expect(audio.seeks).toEqual([1e101, 0]);
  });

  it("leaves a file that knows its length alone", () => {
    const audio = new FakeAudio();
    audio.duration = 3;
    fullDuration(audio as unknown as HTMLMediaElement);
    audio.fire("loadedmetadata");
    expect(audio.seeks).toEqual([]);
  });

  it("stops listening when the player goes", () => {
    const audio = new FakeAudio();
    fullDuration(audio as unknown as HTMLMediaElement).destroy();
    audio.fire("loadedmetadata");
    expect(audio.seeks).toEqual([]);
  });
});
