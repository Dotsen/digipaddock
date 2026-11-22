import { afterEach, describe, expect, it, vi } from "vitest";
import { resyncAll } from "../live.svelte";
import { projects } from "./projects.svelte";

function json(body: unknown) {
  return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
}

const digipaddock = (name: string) => ({ id: "digipaddock", name, route: "@dev", repo: "" });

function serve(list: unknown[]) {
  const fetch = vi.fn(async (url: string) => json(url === "/api/projects" ? list : []));
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

afterEach(() => {
  projects.reset();
  vi.unstubAllGlobals();
});

describe("projects (KD-9064)", () => {
  // milestones are gone — the store reads the projects and whom a card can be given to
  it("asks for no milestones", async () => {
    const fetch = serve([digipaddock("DigiPaddock")]);
    await projects.ensure();
    expect(fetch.mock.calls.map(([url]) => url).sort()).toEqual(["/api/assignees", "/api/projects"]);
    expect(projects).not.toHaveProperty("milestones");
  });

  it("names a project by its name, by its id until the list is read", async () => {
    expect(projects.name("digipaddock")).toBe("digipaddock");
    serve([digipaddock("DIGIPADDOCK")]);
    await projects.ensure();
    expect(projects.name("digipaddock")).toBe("DIGIPADDOCK");
    expect(projects.name("gone")).toBe("gone");
  });

  it("a refresh before the first read asks nothing", async () => {
    const fetch = serve([]);
    await projects.refresh();
    expect(fetch).not.toHaveBeenCalled(); // ensure() reads it when a screen needs it
  });

  it("the first read arriving after a refresh does not bring the old name back", async () => {
    let release: (r: Response) => void = () => {};
    const slow = new Promise<Response>((resolve) => (release = resolve));
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => (url === "/api/projects" ? slow : Promise.resolve(json([])))),
    );
    const first = projects.ensure();
    serve([digipaddock("DIGIPADDOCK")]);
    await projects.refresh();
    release(json([digipaddock("Digipaddock")]));
    await first;
    expect(projects.name("digipaddock")).toBe("DIGIPADDOCK");
  });

  it("a resync reads the list again while followed, and stops with the follow", async () => {
    serve([digipaddock("Digipaddock")]);
    await projects.ensure();
    const stop = projects.follow();
    serve([digipaddock("DIGIPADDOCK")]);
    resyncAll();
    await vi.waitFor(() => expect(projects.name("digipaddock")).toBe("DIGIPADDOCK"));
    stop();
    const fetch = serve([digipaddock("Другое")]);
    resyncAll(); // the chats are read again, the projects no more
    await Promise.resolve();
    expect(fetch.mock.calls.map(([url]) => url)).not.toContain("/api/projects");
    expect(projects.name("digipaddock")).toBe("DIGIPADDOCK");
  });
});
