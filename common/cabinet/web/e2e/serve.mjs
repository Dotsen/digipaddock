// The e2e backend: a fresh, seeded dev cabinet. Usage: node e2e/serve.mjs <port> <state dir> [ru|en] — the
// cabinet's default language, ru by default: the browser tests read the interface in Russian.
// The state is wiped first, so every run starts from the same seed; the sign-in codes land in
// <state dir>/codes.json. Python comes from KD_PYTHON or the repository's tmp/venv (see the stage protocol).
import { spawn } from "node:child_process";
import { mkdirSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const [port, state, language = "ru"] = process.argv.slice(2);
if (!port || !state) {
  console.error("usage: node e2e/serve.mjs <port> <state dir> [ru|en]");
  process.exit(2);
}
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../..");
const python =
  process.env.KD_PYTHON ??
  path.join(root, "tmp", "venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");

const stateRoot = path.resolve(root, "tmp", "cabinet-dev");
const statePath = path.resolve(state);
if (!statePath.startsWith(stateRoot + path.sep)) throw new Error("E2E state must be inside tmp/cabinet-dev");
rmSync(statePath, { recursive: true, force: true });
mkdirSync(state, { recursive: true });
const child = spawn(
  python,
  [
    "-I",
    path.join(root, "common", "bin", "kd-cabinet"),
    "serve",
    "--dev",
    "--seed",
    "--db",
    path.join(state, "cabinet.db"),
    "--state-dir",
    state,
    "--listen",
    `127.0.0.1:${port}`,
    "--language",
    language,
    "--origin",
    `http://localhost:${port}`,
  ],
  { stdio: "inherit" },
);
for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => child.kill());
child.on("exit", (code) => process.exit(code ?? 0));
