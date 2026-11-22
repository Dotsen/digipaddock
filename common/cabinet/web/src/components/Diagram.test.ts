import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ANSWER_MS } from "../lib/diagram";
import { setLocale } from "../lib/i18n.svelte";
import Diagram from "./Diagram.svelte";
import Markdown from "./Markdown.svelte";

const CODE = "flowchart TD\n  A[Запрос] --> B{Тип?}";

beforeEach(() => setLocale("ru"));
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function mount(code = CODE) {
  const view = render(Diagram, { code });
  const frame = view.container.querySelector("iframe") as HTMLIFrameElement;
  const sent = vi.spyOn(frame.contentWindow as Window, "postMessage").mockImplementation(() => undefined);
  const answer = (data: unknown, source: unknown = frame.contentWindow) =>
    fireEvent(window, Object.defineProperty(new MessageEvent("message", { data }), "source", { value: source }));
  return { ...view, frame, sent, answer };
}

describe("Diagram", () => {
  it("is a sandboxed frame that gets the block's text once it has loaded", async () => {
    const { frame, sent } = mount();
    expect(frame.getAttribute("src")).toBe("/diagram.html");
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts"); // no allow-same-origin: the frame has no session
    expect(frame.getAttribute("title")).toBe("Схема");
    expect(screen.getByText("Рисуем схему…")).toBeTruthy();
    expect(sent).not.toHaveBeenCalled();
    await fireEvent.load(frame);
    expect(sent).toHaveBeenCalledTimes(1);
    expect(sent.mock.calls[0][0]).toMatchObject({ type: "render", id: 1, code: CODE, scheme: "dark" });
    expect(sent.mock.calls[0][1]).toBe("*");
  });

  it("takes the drawing's height from the frame, and only from its own frame", async () => {
    const { frame, answer } = mount();
    await fireEvent.load(frame);
    await answer({ type: "rendered", id: 1, height: 300 }, window);
    expect(frame.style.height).toBe("0px");
    await answer({ type: "rendered", id: 1, height: 211.2, html: "<img onerror=alert(1)>" });
    expect(frame.style.height).toBe("212px");
    expect(frame.classList.contains("off")).toBe(false);
    expect(screen.queryByText("Рисуем схему…")).toBeNull();
    expect(document.querySelector("img")).toBeNull();
  });

  it("shows the block's text and the reason when mermaid cannot draw it, and the drawing after a fix", async () => {
    const { frame, answer, sent, rerender } = mount("flowchart ???");
    await fireEvent.load(frame);
    await answer({ type: "error", id: 1, message: "Parse error on line 1:\nflowchart ???\n----------^" });
    expect(screen.getByText("Схема не нарисована, ниже — её текст · Parse error on line 1")).toBeTruthy();
    expect(document.querySelector("pre code")?.textContent).toBe("flowchart ???");
    expect(frame.classList.contains("off")).toBe(true);
    await rerender({ code: CODE });
    expect(sent.mock.calls.at(-1)?.[0]).toMatchObject({ type: "render", id: 2, code: CODE });
    await answer({ type: "rendered", id: 1, height: 300 }); // about the earlier text: not an answer
    expect(document.querySelector("pre")).not.toBeNull();
    await answer({ type: "rendered", id: 2, height: 120 });
    expect(document.querySelector("pre")).toBeNull();
    expect(frame.style.height).toBe("120px");
  });

  it("a frame that stays silent leaves the block's text", async () => {
    vi.useFakeTimers();
    const { frame } = mount();
    await fireEvent.load(frame);
    await vi.advanceTimersByTimeAsync(ANSWER_MS + 1);
    expect(screen.getByText("Схема не нарисована, ниже — её текст · нет ответа")).toBeTruthy();
    expect(document.querySelector("pre code")?.textContent).toBe(CODE);
  });
});

describe("Markdown", () => {
  it("puts a diagram between the pieces of a text and leaves an inline text alone", () => {
    const { container } = render(Markdown, { text: `Цель\n---\nдо схемы\n\n\`\`\`mermaid\n${CODE}\n\`\`\`\n\nпосле схемы` });
    const kids = [...(container.querySelector(".md") as HTMLElement).children].map((kid) => kid.tagName);
    expect(kids).toEqual(["H2", "P", "DIV", "P"]);
    expect(container.querySelectorAll("iframe")).toHaveLength(1);
    cleanup();
    const inline = render(Markdown, { text: "`code` и KD-5", inline: true });
    expect(inline.container.querySelector("iframe")).toBeNull();
    expect(inline.container.querySelector(".md.inline code")?.textContent).toBe("code");
  });
});
