import { beforeEach, describe, expect, it, vi } from "vitest";
import { goBack, handleLinkClick, match, navigate, parentOf, router, startRouter } from "./router.svelte";

describe("match", () => {
  it("names the screens and their parameters", () => {
    expect(match("/inbox")).toMatchObject({ name: "inbox", params: {} });
    expect(match("/cards/KD-5")).toMatchObject({ name: "card", params: { id: "KD-5" } });
    expect(match("/decisions/0123abcd")).toMatchObject({
      name: "decision",
      params: { id: "0123abcd" },
    });
    // the old BOSS page's links (nginx: /boss/promotions/<id> -> here) find the decision by its proposal
    expect(match(`/decisions/by-proposal/${"a".repeat(32)}`)).toMatchObject({
      name: "decision",
      params: { proposal: "a".repeat(32) },
    });
    expect(match("/agents/dev")).toMatchObject({ name: "agent", params: { name: "dev" } });
    expect(match("/settings/users")).toMatchObject({ name: "users" });
    expect(match("/settings/projects")).toMatchObject({ name: "projects" });
    expect(match("/settings/models")).toMatchObject({ name: "models" });
    // the servers section
    expect(match("/servers")).toMatchObject({ name: "servers", params: {} });
    expect(match("/servers/node-f")).toMatchObject({ name: "server", params: { alias: "node-f" } });
    expect(match("/incidents/P-35")).toMatchObject({ name: "incident", params: { id: "P-35" } });
    // the monitor's settings — before the server page, «settings» is no alias
    expect(match("/servers/settings?server=node-c")).toMatchObject({ name: "monitor-settings", params: {} });
    expect(match("/incidents/35").name).toBe("notfound");
  });

  it("opens the overview at the root, the inbox at /inbox, and keeps the query", () => {
    expect(match("/").name).toBe("dashboard");
    expect(match("/?project=digipaddock&period=7d").query.get("period")).toBe("7d");
    expect(match("/inbox").name).toBe("inbox");
    const route = match("/board?project=digipaddock&status=Review");
    expect(route.name).toBe("board");
    expect(route.query.get("project")).toBe("digipaddock");
    expect(route.path).toBe("/board?project=digipaddock&status=Review");
  });

  it("decodes parameters and refuses unknown paths", () => {
    expect(match("/agents/a%20b").params.name).toBe("a b");
    expect(match("/cards/KD-%E0%A4%A").params.id).toBe("KD-%E0%A4%A"); // not throwable at the page
    expect(match("/nowhere").name).toBe("notfound");
    expect(match("/cards/").name).toBe("notfound");
  });
});

describe("navigate", () => {
  beforeEach(() => {
    history.replaceState(null, "", "/inbox");
    startRouter();
  });

  it("pushes history and updates the current route", () => {
    navigate("/cards/KD-7");
    expect(location.pathname).toBe("/cards/KD-7");
    expect(router.current).toMatchObject({ name: "card", params: { id: "KD-7" } });
  });

  it("replaces history when asked", () => {
    const before = history.length;
    navigate("/board", { replace: true });
    expect(history.length).toBe(before);
    expect(router.current.name).toBe("board");
  });

  it("follows the browser's back button", () => {
    history.pushState(null, "", "/feed");
    dispatchEvent(new PopStateEvent("popstate"));
    expect(router.current.name).toBe("feed");
  });

  it("counts the steps taken inside the cabinet in the history entry (KD-9105)", () => {
    expect(router.depth).toBe(0);
    navigate("/board?status=Review");
    navigate("/cards/KD-7");
    expect(router.depth).toBe(2);
    expect(history.state).toEqual({ kdDepth: 2 });
    navigate("/cards/KD-7?tab=brief", { replace: true });
    expect(router.depth).toBe(2);
    history.replaceState({ kdDepth: 1 }, "", "/board?status=Review");
    dispatchEvent(new PopStateEvent("popstate"));
    expect(router.depth).toBe(1);
    history.replaceState(null, "", "/cards/KD-7");
    startRouter();
    expect(router.depth).toBe(0); // a tab opened on a link
  });

  it("goes back in the tab when it got here inside the cabinet", () => {
    navigate("/board?assignee=%40dev");
    navigate("/cards/KD-7");
    const back = vi.spyOn(history, "back").mockImplementation(() => undefined);
    goBack();
    expect(back).toHaveBeenCalledOnce();
  });

  it("goes to the parent, not out of the cabinet, on a tab opened on a link", () => {
    const back = vi.spyOn(history, "back").mockImplementation(() => undefined);
    for (const [path, parent] of [
      ["/cards/KD-7", "/board"],
      ["/cards/KD-7?decision=abc", "/inbox"],
      ["/agents/dev", "/agents"],
      ["/settings/users", "/settings"],
      ["/settings/projects", "/settings"],
      ["/settings/models", "/settings"],
    ]) {
      history.replaceState(null, "", path);
      startRouter();
      goBack();
      expect(location.pathname + location.search).toBe(parent);
    }
    expect(back).not.toHaveBeenCalled();
  });

  it("names the parent of the pages with «Назад» and none for the menu's sections", () => {
    expect(parentOf(match("/decisions/abc"))).toBe("/inbox");
    for (const path of ["/", "/inbox", "/board", "/agents", "/feed", "/settings", "/servers"]) {
      expect(parentOf(match(path))).toBeNull();
    }
    expect(parentOf(match("/servers/node-c"))).toBe("/servers");
    expect(parentOf(match("/incidents/P-1"))).toBe("/incidents");
    expect(parentOf(match("/servers/settings"))).toBe("/servers");
  });

  it("takes over same-origin links and leaves others alone", () => {
    const inner = document.createElement("a");
    inner.href = "/agents/boss";
    document.body.append(inner);
    const click = new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 });
    inner.dispatchEvent(click);
    expect(handleLinkClick(click)).toBe(true);
    expect(router.current).toMatchObject({ name: "agent", params: { name: "boss" } });

    // not dispatched: jsdom would try to leave the page; the handler only reads the target
    const clickOn = (href: string, init: MouseEventInit = {}) => {
      const link = document.createElement("a");
      link.href = href;
      const event = new MouseEvent("click", { cancelable: true, button: 0, ...init });
      Object.defineProperty(event, "target", { value: link });
      return event;
    };
    expect(handleLinkClick(clickOn("https://example.org/x"))).toBe(false);
    expect(handleLinkClick(clickOn("/api/files/1"))).toBe(false);
    expect(handleLinkClick(clickOn("/feed", { ctrlKey: true }))).toBe(false);
    expect(router.current.name).toBe("agent");
    // an anchor on the page shown (the overview's counts) is the browser's: it scrolls, the route stays
    expect(handleLinkClick(clickOn(`${location.pathname}${location.search}#needs`))).toBe(false);
    expect(router.current.name).toBe("agent");
  });
});
