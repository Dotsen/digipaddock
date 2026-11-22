// A project's Hindsight bank as the cabinet takes it (kd.memory_bank.BANK): Latin letters, digits, "_",
// ".", ":" and "-", up to 100, not starting with ".", "-" or ":"; "" — the assignee's role bank. The form checks it
// before any request; the server, the session host and agent-launch check it again.
export const BANK_PATTERN = "[A-Za-z0-9_][A-Za-z0-9_.:\\-]{0,99}";
const BANK = new RegExp(`^${BANK_PATTERN}$`);

export function validBank(value: string): boolean {
  return value === "" || BANK.test(value);
}
