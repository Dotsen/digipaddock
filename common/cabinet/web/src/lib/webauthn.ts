// Passkeys in the browser. The server sends WebAuthn options as JSON (py_webauthn's options_to_json) and
// takes the credential back as JSON. Browsers with parse*OptionsFromJSON / toJSON use them; others (older
// Safari) get the base64url <-> ArrayBuffer conversion done here.

import type { Key } from "./i18n.svelte";

type Json = Record<string, any>;

export function toBase64url(buffer: ArrayBuffer | ArrayBufferView): string {
  const bytes =
    buffer instanceof ArrayBuffer
      ? new Uint8Array(buffer)
      : new Uint8Array(buffer.buffer, buffer.byteOffset, buffer.byteLength);
  let text = "";
  for (const byte of bytes) text += String.fromCharCode(byte);
  return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function fromBase64url(text: string): ArrayBuffer {
  const base64 = text.replace(/-/g, "+").replace(/_/g, "/");
  const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
  const raw = atob(padded);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes.buffer;
}

function descriptors(list: Json[] | undefined): PublicKeyCredentialDescriptor[] | undefined {
  return list?.map((item) => ({ ...item, id: fromBase64url(item.id) }) as PublicKeyCredentialDescriptor);
}

export function creationOptionsFromJson(options: Json): PublicKeyCredentialCreationOptions {
  return {
    ...options,
    challenge: fromBase64url(options.challenge),
    user: { ...options.user, id: fromBase64url(options.user.id) },
    excludeCredentials: descriptors(options.excludeCredentials),
  } as PublicKeyCredentialCreationOptions;
}

export function requestOptionsFromJson(options: Json): PublicKeyCredentialRequestOptions {
  return {
    ...options,
    challenge: fromBase64url(options.challenge),
    allowCredentials: descriptors(options.allowCredentials),
  } as PublicKeyCredentialRequestOptions;
}

function credentialToJson(credential: PublicKeyCredential): Json {
  const withJson = credential as PublicKeyCredential & { toJSON?: () => Json };
  if (typeof withJson.toJSON === "function") return withJson.toJSON();
  const response = credential.response as AuthenticatorResponse & Json;
  const out: Json = { clientDataJSON: toBase64url(response.clientDataJSON) };
  if ("attestationObject" in response) {
    out.attestationObject = toBase64url(response.attestationObject);
    if (typeof response.getTransports === "function") out.transports = response.getTransports();
  } else {
    out.authenticatorData = toBase64url(response.authenticatorData);
    out.signature = toBase64url(response.signature);
    if (response.userHandle) out.userHandle = toBase64url(response.userHandle);
  }
  return {
    id: credential.id,
    rawId: toBase64url(credential.rawId),
    type: credential.type,
    response: out,
    authenticatorAttachment: credential.authenticatorAttachment ?? undefined,
    clientExtensionResults: credential.getClientExtensionResults(),
  };
}

type ParsingPkc = typeof PublicKeyCredential & {
  parseCreationOptionsFromJSON?: (o: Json) => PublicKeyCredentialCreationOptions;
  parseRequestOptionsFromJSON?: (o: Json) => PublicKeyCredentialRequestOptions;
};

export function isPasskeyAvailable(): boolean {
  return typeof window !== "undefined" && "PublicKeyCredential" in window && !!navigator.credentials;
}

export async function registerPasskey(options: Json): Promise<Json> {
  const pkc = PublicKeyCredential as ParsingPkc;
  const publicKey = pkc.parseCreationOptionsFromJSON
    ? pkc.parseCreationOptionsFromJSON(options)
    : creationOptionsFromJson(options);
  const credential = (await navigator.credentials.create({ publicKey })) as PublicKeyCredential;
  return credentialToJson(credential);
}

export async function assertPasskey(options: Json): Promise<Json> {
  const pkc = PublicKeyCredential as ParsingPkc;
  const publicKey = pkc.parseRequestOptionsFromJSON
    ? pkc.parseRequestOptionsFromJSON(options)
    : requestOptionsFromJson(options);
  const credential = (await navigator.credentials.get({ publicKey })) as PublicKeyCredential;
  return credentialToJson(credential);
}

/** The browser's "cancelled or timed out" (NotAllowedError) — a person's choice, not a fault. */
export function isCancelled(error: unknown): boolean {
  return error instanceof DOMException && (error.name === "NotAllowedError" || error.name === "AbortError");
}

/** The message key of a passkey prompt's failure a person can act on, or null for anything else: cancelled, or the
 * chosen authenticator already holds a key of this cabinet (``excludeCredentials`` → InvalidStateError). */
export function passkeyErrorKey(error: unknown): Key | null {
  if (isCancelled(error)) return "error.passkey_cancelled";
  if (error instanceof DOMException && error.name === "InvalidStateError") return "error.passkey_exists";
  return null;
}
