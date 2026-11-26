import { describe, expect, it } from "vitest";
import { setNames } from "./i18n.svelte";
import { renderInline, renderMarkdown, renderParts } from "./markdown";

describe("markdown", () => {
  it("renders the usual markdown", () => {
    const html = renderMarkdown("**bold** and `code`\n\n- one\n- two");
    expect(html).toContain("<strong>bold</strong>");
    expect(html).toContain("<code>code</code>");
    expect(html).toContain("<li>one</li>");
  });

  it("renders a brief as agents pass it in an argument: titles underlined with ---, names in backticks (KD-9329)", () => {
    const html = renderMarkdown(
      "Цель\n---\nПартнёр `sales` заводит клиентов: `POST /partner/v1/clients/{client_id}`.\n\n" +
        "Сейчас\n---\n- типа нет, только `restaurant_scope`;\n- предел — `max_users`.\n\n" +
        "| Путь | Ответ |\n|---|---|\n| `/clients` | `403` |\n",
    );
    expect(html).toContain("<h2>Цель</h2>");
    expect(html).toContain("<h2>Сейчас</h2>");
    expect(html).toContain("<code>POST /partner/v1/clients/{client_id}</code>");
    expect(html).toContain("<li>типа нет, только <code>restaurant_scope</code>;</li>");
    expect(html).toContain("<td><code>/clients</code></td>");
    expect(renderMarkdown("### Решено по умолчанию\n\nТекст")).toContain("<h3>Решено по умолчанию</h3>");
  });

  it("shows raw HTML as text, never as markup", () => {
    const html = renderMarkdown('<img src=x onerror="alert(1)"> <script>alert(2)</script>');
    expect(html).not.toContain("<img");
    expect(html).not.toContain("<script");
    expect(html).toContain("&lt;img");
  });

  it("drops script links and opens other links in a new tab", () => {
    const bad = renderMarkdown("[x](javascript:alert(1))");
    expect(bad).not.toContain("javascript:");
    const good = renderMarkdown("[docs](https://example.org/a)");
    expect(good).toContain('href="https://example.org/a"');
    expect(good).toContain('rel="noopener noreferrer"');
    expect(good).toContain('target="_blank"');
  });

  it("drops a link a browser reads as another host: a backslash after the first slash, also as an entity", () => {
    const backslash = String.fromCharCode(92);
    for (const href of ["/\\evil.example", "/&#92;evil.example", "/&bsol;/evil.example", "//evil.example", "/&#9;/evil.example"]) {
      const html = renderMarkdown(`[x](${href})`);
      // marked may percent-encode a literal backslash, which is safe; a raw one or `//` is not
      expect(html, href).not.toContain('href="/' + backslash);
      expect(html, href).not.toContain('href="//');
    }
  });

  it("keeps a link inside the cabinet in the same tab", () => {
    const html = renderMarkdown("[KD-5](/cards/KD-5)");
    expect(html).toContain('href="/cards/KD-5"');
    expect(html).not.toContain("_blank");
  });

  it("turns a card number in text into a link to its card", () => {
    const html = renderMarkdown("Сделано: KD-227, дальше KD-9038.");
    expect(html).toBe(
      '<p>Сделано: <a href="/cards/KD-227">KD-227</a>, дальше <a href="/cards/KD-9038">KD-9038</a>.</p>\n',
    );
    expect(renderInline("**KD-5** готова")).toBe('<strong><a href="/cards/KD-5">KD-5</a></strong> готова');
  });

  it("leaves card numbers in links, code, paths and words alone", () => {
    const linked = renderMarkdown("[карточка KD-5](/cards/KD-5)");
    expect(linked.match(/<a /g)).toHaveLength(1);
    const url = renderMarkdown("https://cabinet.example.test/cards/KD-5");
    expect(url.match(/<a /g)).toHaveLength(1);
    expect(renderMarkdown("`KD-5` and\n\n```\nKD-6\n```")).not.toContain('href="/cards/');
    expect(renderInline("/cards/KD-5 XKD-5 KD-5a KD-5-1 kd-5 вKD-5 KD-5я")).not.toContain("<a ");
  });

  it("links the number of the old «KD-n (url)» form once, the address stays its own link", () => {
    const html = renderInline("KD-227 (https://cabinet.example.test/cards/KD-227)");
    expect(html).toContain('<a href="/cards/KD-227">KD-227</a>');
    expect(html.match(/<a /g)).toHaveLength(2);
  });

  it("renders a single line without a paragraph", () => {
    expect(renderInline("Работает на **десктопе**")).toBe("Работает на <strong>десктопе</strong>");
    expect(renderInline("<b>x</b>")).toBe("&lt;b&gt;x&lt;/b&gt;");
  });

  it("links the host's card IDs (KD-9184)", () => {
    setNames("acme", "AC");
    try {
      expect(renderInline("AC-12 и KD-3")).toBe('<a href="/cards/AC-12">AC-12</a> и KD-3');
    } finally {
      setNames();
    }
    expect(renderInline("KD-3")).toBe('<a href="/cards/KD-3">KD-3</a>');
  });

  it("links a card typed with an earlier prefix of the host to its card (KD-9184)", () => {
    setNames("kd", "KD", ["OLD"]);
    try {
      expect(renderInline("OLD-5 и KD-6")).toBe('<a href="/cards/KD-5">OLD-5</a> и <a href="/cards/KD-6">KD-6</a>');
      expect(renderInline("XOLD-5 OLD-5a")).not.toContain("<a ");
    } finally {
      setNames();
    }
    expect(renderInline("OLD-5")).toBe("OLD-5");
  });

  it("splits a text at its diagrams: a ```mermaid block of its own is not HTML (KD-9330)", () => {
    const flow = "flowchart TD\n  A[<b>Запрос</b>] --> B{Тип?}";
    const parts = renderParts(
      "Цель\n---\nдо [схемы][s], KD-5\n\n```mermaid\n" + flow + "\n```\n\nпосле `схемы`\n\n```Mermaid title\nstateDiagram-v2\n```\n\n[s]: https://example.test/s\n",
    );
    expect(parts.map((part) => part.kind)).toEqual(["html", "diagram", "html", "diagram"]);
    expect(parts[1]).toEqual({ kind: "diagram", code: flow }); // the text as written: the frame draws it, not the page
    expect(parts[3]).toEqual({ kind: "diagram", code: "stateDiagram-v2" });
    const before = (parts[0] as { html: string }).html;
    expect(before).toContain("<h2>Цель</h2>");
    expect(before).toContain('<a href="https://example.test/s" target="_blank" rel="noopener noreferrer">схемы</a>');
    expect(before).toContain('<a href="/cards/KD-5">KD-5</a>');
    expect((parts[2] as { html: string }).html).toBe("<p>после <code>схемы</code></p>\n");
  });

  it("leaves a text without a diagram, and a block that is not one, as they were", () => {
    for (const text of [
      "**bold** and `code`\n\n- one\n- two",
      "```js\nlet a = 1;\n```",
      "```mermaidx\nflowchart TD\n```",
      "```mermaid\n\n```", // an empty block has nothing to draw
      "- шаг:\n  ```mermaid\n  flowchart TD\n  ```", // inside a list it is the list's code block
      "> ```mermaid\n> flowchart TD\n> ```",
      "<script>alert(1)</script>",
      "см. [спеку][s] и KD-5\n\n[s]: https://example.test/s",
    ]) {
      expect(renderParts(text), text).toEqual([{ kind: "html", html: renderMarkdown(text) }]);
    }
    expect(renderParts("")).toEqual([]);
    // a text of many blocks draws its first eight; the rest stay code blocks
    const many = renderParts(Array.from({ length: 11 }, (_, n) => "```mermaid\nflowchart TD\n  A --> B" + n + "\n```").join("\n\n"));
    expect(many.filter((part) => part.kind === "diagram")).toHaveLength(8);
    expect((many.at(-1) as { html: string }).html.match(/<pre>/g)).toHaveLength(3);
    expect(renderParts("```mermaid\nflowchart TD\n```")).toEqual([{ kind: "diagram", code: "flowchart TD" }]);
  });
});
