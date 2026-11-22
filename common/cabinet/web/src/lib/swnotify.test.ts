import { describe, expect, it } from "vitest";
import { notificationOf } from "./swnotify";

describe("the notification a push shows (KD-9095)", () => {
  it("a tagged push replaces the older one with the same tag and alerts again", () => {
    const { title, options } = notificationOf({ title: "KD-1", body: "Ждёт решения", tag: "KD-1", url: "/cards/KD-1" });
    expect(title).toBe("KD-1");
    expect(options).toMatchObject({ body: "Ждёт решения", tag: "KD-1", renotify: true, data: { url: "/cards/KD-1" } });
  });

  it("an untagged push has no renotify: the browser refuses renotify without a tag", () => {
    const { title, options } = notificationOf({});
    expect(title).toBe("DIGIPADDOCK");
    expect(options.tag).toBeUndefined();
    expect(options.renotify).toBeUndefined();
    expect(options.data).toEqual({ url: "/inbox" });
  });

  it("the status-bar badge is the monochrome one: Android keeps only its alpha, an opaque icon is a white square", () => {
    expect(notificationOf({}).options).toMatchObject({ icon: "/icons/icon-192.v2.png", badge: "/icons/badge-96.png" });
  });

  it("a link to another origin opens the inbox instead", () => {
    expect(notificationOf({ url: "//evil.example/x" }).options.data).toEqual({ url: "/inbox" });
  });
});
