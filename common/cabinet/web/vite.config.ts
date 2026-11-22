// The cabinet's PWA build. In dev, `vite` proxies /api and /ws to `kd-cabinet serve --dev`;
// `npm run build` writes dist/ that the backend (dev) or nginx (stage) serves.
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";
import { VitePWA } from "vite-plugin-pwa";

const BACKEND = "http://127.0.0.1:8765";
const here = path.dirname(fileURLToPath(import.meta.url));

// The diagram frame: /diagram.html and its two classic scripts — mermaid's prebuilt bundle and
// src/diagram/frame.js — copied as they are. Not a part of the app's bundle: the frame is another document with
// another policy (nginx), and the 2.8 MB of mermaid load only where a text has a diagram.
const DIAGRAM = {
  page: path.join(here, "src/diagram/frame.html"),
  mermaid: path.join(here, "node_modules/@mermaid-js/tiny/dist/mermaid.tiny.js"),
  frame: path.join(here, "src/diagram/frame.js"),
};
const framePage = (mermaid: string, frame: string) =>
  readFileSync(DIAGRAM.page, "utf-8").replace("%MERMAID%", mermaid).replace("%FRAME%", frame);

function diagramFrame(): Plugin {
  return {
    name: "cabinet-diagram-frame",
    generateBundle() {
      const asset = (name: string, file: string) =>
        `/${this.getFileName(this.emitFile({ type: "asset", name, source: readFileSync(file) }))}`;
      const page = framePage(asset("mermaid.js", DIAGRAM.mermaid), asset("diagram-frame.js", DIAGRAM.frame));
      this.emitFile({ type: "asset", fileName: "diagram.html", source: page });
    },
    // `npm run dev`: the same page, its scripts straight from their sources
    configureServer(server) {
      const scripts: Record<string, string> = { "/diagram/mermaid.js": DIAGRAM.mermaid, "/diagram/frame.js": DIAGRAM.frame };
      server.middlewares.use((request, response, next) => {
        const url = (request.url ?? "").split("?")[0];
        if (url === "/diagram.html") {
          response.setHeader("Content-Type", "text/html; charset=utf-8");
          response.end(framePage("/diagram/mermaid.js", "/diagram/frame.js"));
        } else if (url in scripts) {
          response.setHeader("Content-Type", "text/javascript; charset=utf-8");
          response.end(readFileSync(scripts[url]));
        } else {
          next();
        }
      });
    },
  };
}

export default defineConfig({
  plugins: [
    svelte(),
    diagramFrame(),
    VitePWA({
      strategies: "injectManifest",
      srcDir: "src",
      filename: "sw.ts",
      // a deferred <script src> — never an inline script (the page's CSP has no 'unsafe-inline')
      injectRegister: "script-defer",
      registerType: "autoUpdate",
      manifest: false, // public/manifest.webmanifest is the manifest
      injectManifest: {
        globPatterns: ["**/*.{js,css,html,svg,png,ico,webmanifest}"],
        // the diagram frame is not the app: the worker neither precaches mermaid nor serves the frame
        globIgnores: ["diagram.html", "assets/mermaid-*.js", "assets/diagram-frame-*.js"],
      },
      devOptions: { enabled: false },
    }),
  ],
  build: {
    target: "es2022",
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": BACKEND,
      "/ws": { target: BACKEND.replace("http", "ws"), ws: true },
    },
  },
});
