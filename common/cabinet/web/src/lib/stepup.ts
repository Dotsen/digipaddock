// Step-up for a sensitive change (server: web/stepup.py). The change is tried as is; when the server
// answers 403 stepup_required, a challenge for that purpose is confirmed with a passkey (or, without one
// or when its prompt is dismissed, a TOTP code from `askTotp`) and the change is sent again with
// `stepup: {challenge_id, credential|totp}`.
import { ApiError, apiPost } from "./api";
import { assertPasskey, isCancelled } from "./webauthn";

export type Proof = { challenge_id: string; credential?: unknown; totp?: string };
export type AskTotp = () => Promise<string | null>;

interface Challenge {
  challenge_id: string;
  options: Record<string, unknown> | null;
  totp: boolean;
}

/** A proof for `purpose`, or null when the person has only TOTP and gives no code. */
export async function proveStepUp(purpose: string, askTotp?: AskTotp): Promise<Proof | null> {
  const challenge = await apiPost<Challenge>("/api/stepup/challenge", { purpose });
  if (challenge.options) {
    try {
      const credential = await assertPasskey(challenge.options);
      return { challenge_id: challenge.challenge_id, credential };
    } catch (exc) {
      // the key may be on another device: a person with TOTP confirms with a code instead
      if (!(isCancelled(exc) && challenge.totp && askTotp)) throw exc;
    }
  }
  const code = challenge.totp && askTotp ? await askTotp() : null;
  return code ? { challenge_id: challenge.challenge_id, totp: code } : null;
}

export async function withStepUp<T>(
  purpose: string,
  run: (proof?: Proof) => Promise<T>,
  askTotp?: AskTotp,
): Promise<T> {
  try {
    return await run(undefined);
  } catch (exc) {
    if (!(exc instanceof ApiError) || exc.kind !== "stepup_required") throw exc;
    const proof = await proveStepUp(purpose, askTotp);
    if (!proof) throw exc;
    return run(proof);
  }
}
