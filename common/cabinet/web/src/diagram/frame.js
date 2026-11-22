// The diagram frame's script: /diagram.html is mermaid's own build and this file, two classic scripts.
// The cabinet embeds the page sandboxed — an opaque origin: no session, no API, no storage of the cabinet — and
// under its own Content-Security-Policy (mermaid draws with inline styles, which the cabinet's policy forbids;
// here nothing leaves the frame: no network, no forms). The page sends one diagram's text, the frame answers with
// the height of the drawing or with why there is none. Plain ES2020, not bundled: a module in an opaque origin
// would need CORS.
(function () {
  "use strict";
  var out = document.getElementById("out");
  var turn = 0;
  var shown = null; // the request the drawing on the page answers

  function tell(message) {
    window.parent.postMessage(message, "*"); // the page reads a height and a line of text, nothing else
  }

  function drawn() {
    if (shown === null || !out.firstElementChild) return;
    tell({ type: "rendered", id: shown, height: Math.ceil(out.getBoundingClientRect().height) });
  }

  window.addEventListener("message", function (event) {
    var data = event.data;
    if (event.source !== window.parent || !data || data.type !== "render" || typeof data.code !== "string") return;
    var mine = ++turn; // a newer text (the editor's preview) wins over a drawing still in progress
    var mermaid = window.mermaid;
    // the frame is see-through only in the page's own colour scheme
    document.documentElement.style.colorScheme = data.scheme === "light" ? "light" : "dark";
    Promise.resolve()
      .then(function () {
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict", // labels are text: no HTML, no links, no click handlers
          suppressErrorRendering: true,
          theme: "base",
          themeVariables: data.theme && typeof data.theme === "object" ? data.theme : {},
        });
        return mermaid.parse(data.code);
      })
      .then(function () {
        return mermaid.render("diagram" + mine, data.code);
      })
      .then(function (result) {
        if (mine !== turn) return;
        out.innerHTML = result.svg;
        // mermaid lets a drawing shrink to the frame's width; below 0.6 of its size the text is too small to read,
        // so a wider one scrolls sideways — as a wide table does
        var svg = out.firstElementChild;
        var natural = svg ? parseFloat(svg.style.maxWidth) : NaN;
        if (natural > 0) svg.style.minWidth = Math.round(natural * 0.6) + "px";
        shown = data.id;
        drawn();
      })
      .catch(function (error) {
        if (mine !== turn) return;
        out.textContent = "";
        shown = null;
        tell({ type: "error", id: data.id, message: String((error && error.message) || error).slice(0, 400) });
      });
  });

  // the drawing scales with the frame's width: a new height follows a resize
  if (typeof ResizeObserver === "function") new ResizeObserver(drawn).observe(out);
})();
