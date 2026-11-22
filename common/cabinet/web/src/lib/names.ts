// The host's names of commands and cards: the catalogs spell them kd-<word> and KD-<n> as the
// repository does, a host's naming.json may name them otherwise — the server's twin is kd.naming.rewrite.
export interface Names {
  commands: string;
  cards: string;
  aliases?: string[]; // the host's earlier card prefixes (naming.json card_aliases)
}

export const CANON: Names = { commands: "kd", cards: "KD" };

const CARD_ID = /^([A-Z][A-Z0-9]{1,5})-([1-9][0-9]{0,5})$/;

/** A card ID of an earlier prefix of the host (an old link) as the current one; any other value as it is. */
export function canonicalCard(id: string, names: Names): string {
  const m = CARD_ID.exec(id);
  return m && names.aliases?.includes(m[1]) ? `${names.cards}-${m[2]}` : id;
}

// a command with the path before it: a file of the release tree (common/bin/kd-task) keeps its name
const COMMAND = /(?<![A-Za-z0-9_.-])((?:[A-Za-z0-9_.@-]*\/)*)kd-(?=[a-z0-9])/g;
const TREE_BIN = /(?:^|\/)(?:common|agents\/[^/]+)\/bin\/$/;
// a card's form (KD-<n>, KD-n, KD-id, KD-target, KD-review) is the host's; a numbered card is the platform's own history
const CARD = /(?<![A-Za-z0-9_])KD-(?=<|(?:[nmN]|id|target|review)\b)/g;

export function rewriteNames(text: string, names: Names): string {
  if (names.commands !== CANON.commands) {
    text = text.replace(COMMAND, (whole, path: string) => (TREE_BIN.test(path) ? whole : `${path}${names.commands}-`));
  }
  if (names.cards !== CANON.cards) text = text.replace(CARD, () => `${names.cards}-`);
  return text;
}
