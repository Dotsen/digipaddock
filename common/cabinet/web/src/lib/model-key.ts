// The root writer issues and verifies this single-use passkey challenge.
import { apiPost } from "./api";
import { assertPasskey } from "./webauthn";

export async function changeProviderKey(provider: string, action: "put" | "delete", key = "") {
  const path = `/api/models/providers/${encodeURIComponent(provider)}/key`;
  const digest = action === "put"
    ? Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(key))),
      (b) => b.toString(16).padStart(2, "0")).join("") : "";
  const challenge = await apiPost<{ challenge_id: string; options: Record<string, unknown> }>(
    `${path}/challenge`, { action, digest });
  const credential = await assertPasskey(challenge.options);
  return apiPost<{ key: boolean; audit: boolean; restart?: boolean }>(path, {
    action, challenge_id: challenge.challenge_id, credential, ...(action === "put" ? { key } : {}),
  });
}
