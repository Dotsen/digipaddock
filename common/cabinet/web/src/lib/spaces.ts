// An agent's threads with people: its chat, and "Monitor" where the monitor's events come and its own
// session answers. A thread's key is the agent's name for the chat and `<agent>/monitor` for "Monitor" — the same
// keys the server uses for the person's unread counts ({"chats": ...}).
export type Space = "chat" | "monitor";
export const SPACES: readonly Space[] = ["chat", "monitor"];

// What a person needs for each thread (server: perms.AGENT_THREAD_PERMS): the chat agents.chat, which no default role
// but the owner's holds; «Монитор» — the monitor's reports on the servers — also servers.read.
export const SPACE_PERMS: Record<Space, readonly string[]> = {
  chat: ["agents.chat"],
  monitor: ["agents.chat", "servers.read"],
};

/** The person may open the agent's thread in `space`; `can` is `session.can` (a grant over every project). */
export function canOpenSpace(can: (perm: string) => boolean, space: Space = "chat"): boolean {
  return SPACE_PERMS[space].every((perm) => can(perm));
}

export function spaceKey(agent: string, space: Space = "chat"): string {
  return space === "chat" ? agent : `${agent}/${space}`;
}

export function parseSpace(key: string): { agent: string; space: Space } {
  const [agent, space] = key.split("/", 2);
  return { agent, space: space === "monitor" ? "monitor" : "chat" };
}

/** The API path of a thread's key: `/api/agents/<agent>` or `/api/agents/<agent>/monitor`. */
export function spaceBase(key: string): string {
  const { agent, space } = parseSpace(key);
  const base = `/api/agents/${encodeURIComponent(agent)}`;
  return space === "chat" ? base : `${base}/${space}`;
}

/** The key of the agent thread a journal event is about (none — not an agent thread's). */
export function eventSpace(payload: Record<string, unknown> | undefined): string | null {
  const agent = payload?.agent;
  if (typeof agent !== "string" || !agent) return null;
  return spaceKey(agent, payload?.space === "monitor" ? "monitor" : "chat");
}

/** The unread replies of all of an agent's threads (its badge in lists and the menu). */
export function agentUnread(all: Record<string, { unread: number } | undefined>, agent: string): number {
  return SPACES.reduce((sum, space) => sum + (all[spaceKey(agent, space)]?.unread ?? 0), 0);
}
