// The frame's script (frame.js runs in /diagram.html as it is, unbundled): here it runs in jsdom with a stand-in
// for mermaid, the window being its own parent.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PAGE from "./frame.html?raw";
import SCRIPT from "./frame.js?raw";

type Mermaid = { initialize: ReturnType<typeof vi.fn>; parse: ReturnType<typeof vi.fn>; render: ReturnType<typeof vi.fn> };
let mermaid: Mermaid;
let told: ReturnType<typeof vi.fn>;
let listener: (event: MessageEvent) => void;

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const ask = (data: unknown, source: unknown = window) => listener({ data, source } as MessageEvent);

beforeEach(() => {
  document.body.innerHTML = '<div id="out"></div>';
  mermaid = {
    initialize: vi.fn(),
    parse: vi.fn(async () => true),
    render: vi.fn(async (id: string) => ({ svg: `<svg id="${id}" style="max-width: 700px"></svg>` })),
  };
  (window as unknown as { mermaid: Mermaid }).mermaid = mermaid;
  told = vi.fn();
  vi.spyOn(window, "postMessage").mockImplementation(told as never);
  vi.spyOn(window, "addEventListener").mockImplementation(((type: string, handler: (event: MessageEvent) => void) => {
    if (type === "message") listener = handler;
  }) as never);
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({ height: 211.2 } as DOMRect);
  new Function(SCRIPT)();
});

afterEach(() => {
  document.documentElement.style.colorScheme = "";
});

describe("the diagram frame", () => {
  it("is a page of mermaid's bundle and this script, with nothing inline to run", () => {
    expect(PAGE.match(/<script[^>]*>/g)).toEqual(['<script src="%MERMAID%">', '<script src="%FRAME%">']);
    expect(PAGE).not.toMatch(/\son[a-z]+=/);
  });

  it("draws the text it is sent, strictly, in the page's colours, and answers with the height", async () => {
    ask({ type: "render", id: 7, code: "flowchart TD\n A --> B", scheme: "light", theme: { primaryColor: "#fff" } });
    await settle();
    expect(mermaid.initialize).toHaveBeenCalledWith({
      startOnLoad: false, securityLevel: "strict", suppressErrorRendering: true, theme: "base",
      themeVariables: { primaryColor: "#fff" },
    });
    expect(mermaid.render).toHaveBeenCalledWith("diagram1", "flowchart TD\n A --> B");
    const svg = document.querySelector("#out svg") as SVGElement;
    expect(svg.style.minWidth).toBe("420px"); // shrinks to 0.6 of its size, a wider one scrolls
    expect(document.documentElement.style.colorScheme).toBe("light");
    expect(told).toHaveBeenCalledWith({ type: "rendered", id: 7, height: 212 }, "*");
  });

  it("answers with the reason when mermaid cannot read the text, and draws nothing", async () => {
    mermaid.parse.mockRejectedValueOnce(new Error("Parse error on line 2:\n...x"));
    ask({ type: "render", id: 8, code: "flowchart ???", scheme: "dark", theme: {} });
    await settle();
    expect(mermaid.render).not.toHaveBeenCalled();
    expect(document.getElementById("out")?.childElementCount).toBe(0);
    expect(told).toHaveBeenCalledWith({ type: "error", id: 8, message: "Parse error on line 2:\n...x" }, "*");
  });

  it("listens to its parent alone and to a render request alone", async () => {
    ask({ type: "render", id: 1, code: "flowchart TD\n A --> B" }, {});
    ask({ type: "rendered", id: 1, height: 5 });
    ask({ type: "render", id: 1, code: 7 });
    ask(null);
    await settle();
    expect(mermaid.initialize).not.toHaveBeenCalled();
    expect(told).not.toHaveBeenCalled();
  });

  it("a newer text wins over a drawing still in progress, and the answer names the text it is about", async () => {
    let first: (value: { svg: string }) => void = () => undefined;
    mermaid.render.mockImplementationOnce(() => new Promise((resolve) => (first = resolve)));
    ask({ type: "render", id: 1, code: "old", scheme: "dark", theme: {} });
    await settle();
    ask({ type: "render", id: 2, code: "new", scheme: "dark", theme: {} });
    await settle();
    first({ svg: '<svg id="stale"></svg>' });
    await settle();
    expect(document.querySelector("#out svg")?.id).toBe("diagram2");
    expect(told).toHaveBeenCalledTimes(1);
    expect(told).toHaveBeenCalledWith({ type: "rendered", id: 2, height: 212 }, "*");
  });
});
