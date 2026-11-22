import { describe, expect, it, vi } from "vitest";
import { openNotification } from "./swclick";

const origin = "https://cabinet.example.test";
const target = `${origin}/incidents/P-471`;
function client(url = `${origin}/cards/KD-212`) {
  const window = {url, navigate:vi.fn(), focus:vi.fn().mockResolvedValue(undefined)};
  window.navigate.mockResolvedValue(window);
  return window;
}
function windows(items:ReturnType<typeof client>[]) {
  return {matchAll:vi.fn().mockResolvedValue(items), openWindow:vi.fn().mockResolvedValue(undefined)};
}

describe("opening incident push links", () => {
  it("navigates an existing or suspended window before focusing it, without relying on page messages", async () => {
    const open = client(), list = windows([open]);
    await openNotification(list, origin, "/incidents/P-471");
    expect(open.navigate).toHaveBeenCalledWith(target);
    expect(open.focus).toHaveBeenCalledOnce();
    expect(list.openWindow).not.toHaveBeenCalled();
  });
  it("opens the deep link when the app is closed or another-origin window is present", async () => {
    const other = client("https://other.example/"), list = windows([other]);
    await openNotification(list, origin, "/incidents/P-471");
    expect(list.openWindow).toHaveBeenCalledWith(target);
    expect(other.navigate).not.toHaveBeenCalled();
  });
  it("falls back from a dead window and uses another open window", async () => {
    const dead = client(), open = client(`${origin}/inbox`), list = windows([dead, open]);
    dead.navigate.mockRejectedValue(new Error("window closed"));
    await openNotification(list, origin, "/incidents/P-471");
    expect(open.navigate).toHaveBeenCalledWith(target);
    expect(list.openWindow).not.toHaveBeenCalled();
  });
  it("opens a new window when navigation returns null", async () => {
    const dead = client(), list = windows([dead]);
    dead.navigate.mockResolvedValue(null);
    await openNotification(list, origin, "/incidents/P-471");
    expect(list.openWindow).toHaveBeenCalledWith(target);
  });
  it("keeps navigation successful if focusing is refused", async () => {
    const open = client(), list = windows([open]);
    open.focus.mockRejectedValue(new Error("focus refused"));
    await openNotification(list, origin, "/incidents/P-471");
    expect(list.openWindow).not.toHaveBeenCalled();
  });
});
