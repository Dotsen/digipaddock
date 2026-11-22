import { describe, expect, it } from "vitest";
import { bannerKey, bannerShown, lastLine, needsInstall } from "./pushview";

describe("push on this device, in words (KD-9043)", () => {
  it("the banner stays until the device is on, and not after «Позже»", () => {
    expect(bannerShown("off", false)).toBe(true);
    expect(bannerShown("lost", false)).toBe(true);
    expect(bannerShown("denied", false)).toBe(true);
    expect(bannerShown("on", false)).toBe(false);
    expect(bannerShown("off", true)).toBe(false);
  });

  it("each state but «on» has its own line", () => {
    expect(bannerKey("off")).toBe("push.banner.off");
    expect(bannerKey("install")).toBe("push.banner.install");
  });

  it("asks to install only where push needs the installed app", () => {
    const iphone = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1";
    expect(needsInstall(iphone, false)).toBe(true);
    expect(needsInstall(iphone, true)).toBe(false);
    expect(needsInstall("Mozilla/5.0 (Windows NT 10.0) Chrome/153.0", false)).toBe(false);
  });

  it("tells the last attempt: none, accepted by the gateway, or an error", () => {
    expect(lastLine(null).key).toBe("push.last.none");
    expect(lastLine({ result: "201", at: "t", sent: true })).toEqual({ key: "push.last.accepted", code: "201", at: "t" });
    expect(lastLine({ result: "429", at: "t", sent: false })).toEqual({ key: "push.last.failed", code: "429", at: "t" });
    expect(lastLine({ result: "0", at: "t", sent: false }).code).toBe("—");
  });
});
