import { afterEach, describe, expect, it } from "vitest";
import { answerOf, renderMessage, themeOf } from "./diagram";

afterEach(() => {
  document.body.innerHTML = "";
  delete document.documentElement.dataset.theme;
});

describe("diagram", () => {
  it("takes mermaid's theme from the cabinet's tokens and leaves an undefined one to mermaid", () => {
    const tokens: Record<string, string> = { "--bg": " #1a1918", "--text": "#ecebe8", "--muted": "#a5a29c" };
    const token = (name: string) => tokens[name] ?? "";
    expect(themeOf(token, "dark")).toEqual({
      darkMode: true, fontSize: "14px", primaryTextColor: "#ecebe8", noteTextColor: "#ecebe8",
      primaryBorderColor: "#a5a29c", lineColor: "#a5a29c", background: "#1a1918", edgeLabelBackground: "#1a1918",
    });
    // a label covers its line with the colour behind the drawing, not the page's
    expect(themeOf(token, "dark", "rgb(28, 36, 51)")).toMatchObject({ background: "rgb(28, 36, 51)", edgeLabelBackground: "rgb(28, 36, 51)" });
    expect(themeOf(() => "", "light")).toEqual({ darkMode: false, fontSize: "14px" });
  });

  it("asks the frame for the block's text in the colours of the place it stands in", () => {
    document.body.innerHTML = '<div style="background-color: rgb(28, 36, 51)"><p style="background-color: rgba(0, 0, 0, 0.3)"><i id="at"></i></p></div>';
    const at = document.getElementById("at") as HTMLElement;
    const message = renderMessage(3, "flowchart TD\n A --> B", at);
    expect(message).toMatchObject({ type: "render", id: 3, code: "flowchart TD\n A --> B", scheme: "dark" });
    expect(message.theme).toMatchObject({ darkMode: true, background: "rgb(28, 36, 51)" }); // a see-through tint is passed over
    document.documentElement.dataset.theme = "light";
    expect(renderMessage(4, "x", at)).toMatchObject({ id: 4, scheme: "light", theme: { darkMode: false } });
  });

  it("reads only the request's number and a height or a reason from the frame's answer", () => {
    expect(answerOf({ type: "rendered", id: 2, height: 211.2, html: "<b>x</b>" })).toEqual({ type: "rendered", id: 2, height: 212 });
    expect(answerOf({ type: "rendered", id: 2, height: 1e9 })).toEqual({ type: "rendered", id: 2, height: 4000 });
    expect(answerOf({ type: "error", id: 2, message: "Parse error on line 2" })).toEqual({ type: "error", id: 2, message: "Parse error on line 2" });
    expect(answerOf({ type: "error", id: 2, message: { toString: () => "x" } })).toEqual({ type: "error", id: 2, message: "" });
    expect((answerOf({ type: "error", id: 2, message: "x".repeat(900) }) as { message: string }).message).toHaveLength(400);
    for (const junk of [null, "rendered", 5, {}, { type: "rendered", id: 2 }, { type: "rendered", id: 2, height: "9" },
      { type: "rendered", id: 2, height: -3 }, { type: "rendered", id: 2, height: NaN }, { type: "rendered", height: 9 },
      { type: "error", id: "2", message: "x" }, { type: "render", id: 2, code: "x" }]) {
      expect(answerOf(junk)).toBeNull();
    }
  });
});
