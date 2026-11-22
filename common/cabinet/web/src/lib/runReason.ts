// Why a run or a session failed, in words: the host and the cabinet store machine codes
// (host_restart, "exit 143", error_during_execution, "refused: …"); the person reads the catalog text and the
// code stays in the element's title. A reason that is already words (a worker job's) is passed as ``unknown``.
import { hasKey, t } from "./i18n.svelte";

// A worker job's runner explains in English (kd.worker_result); its known sentences have catalog words too.
const SENTENCES: [RegExp, (m: RegExpExecArray) => string][] = [
  [/^claude exited (-?\d+)/, (m) => t("run.reason.exit", { code: m[1] })],
  [/^timeout after (\d+) s\b/, (m) => t("run.reason.timeout", { seconds: m[1] })],
  [/^the final block is over (\d+) KiB$/, (m) => t("run.reason.block_too_big", { size: m[1] })],
  [/^the session ended without the specialization's final JSON block$/, () => t("run.reason.no_block")],
  [/^claude reported /, () => t("run.reason.claude_error")],
];

export function reasonText(code: string, unknown?: string): string {
  const exit = /^exit (-?\d+)$/.exec(code.trim());
  if (exit) return t("run.reason.exit", { code: exit[1] });
  for (const [pattern, words] of SENTENCES) {
    const found = pattern.exec(code.trim());
    if (found) return words(found);
  }
  const key = `run.reason.${code.split(":", 1)[0].trim().toLowerCase().replace(/[^a-z0-9_]+/g, "_")}`;
  return key !== "run.reason.exit" && hasKey(key) ? t(key) : (unknown ?? t("run.reason.other"));
}
