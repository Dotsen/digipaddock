// The cabinet's HTTP API: JSON in and out, the session cookie, the CSRF token on writes.
// Errors are ApiError(status, kind, message); kind is the server's `error` field (catalog key error.<kind>).
// A 401 outside /api/auth/ fires the window event "kd:unauthorized": the session forgets the person.

export class ApiError extends Error {
  constructor(
    public status: number,
    public kind: string,
    message: string,
    public body: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export const UNAUTHORIZED = "kd:unauthorized";
const CSRF_HEADER = "X-CSRF-Token";
let csrf = "";

export function setCsrf(token: string) {
  csrf = token;
}

export function csrfToken(): string {
  return csrf;
}

async function parse(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

async function call(method: string, path: string, body?: unknown): Promise<any> {
  const headers = new Headers();
  let payload: BodyInit | undefined;
  if (method !== "GET") headers.set(CSRF_HEADER, csrf);
  if (body instanceof FormData) {
    payload = body;
  } else if (body !== undefined) {
    headers.set("Content-Type", "application/json");
    payload = JSON.stringify(body);
  }
  let response: Response;
  try {
    response = await fetch(path, { method, headers, body: payload, credentials: "same-origin" });
  } catch (exc) {
    throw new ApiError(0, "network", exc instanceof Error ? exc.message : String(exc));
  }
  const data = await parse(response);
  if (response.ok) return data === undefined ? null : data;
  const fields =
    data && typeof data === "object" && !Array.isArray(data) ? (data as Record<string, unknown>) : {};
  const kind = typeof fields.error === "string" ? fields.error : "unknown";
  const message = typeof fields.message === "string" ? fields.message : response.statusText;
  if (response.status === 401 && !path.startsWith("/api/auth/")) {
    dispatchEvent(new CustomEvent(UNAUTHORIZED));
  }
  throw new ApiError(response.status, kind, message, fields);
}

export const apiGet = <T = any>(path: string): Promise<T> => call("GET", path);
export const apiPost = <T = any>(path: string, body: unknown = {}): Promise<T> =>
  call("POST", path, body);
export const apiPatch = <T = any>(path: string, body: unknown): Promise<T> =>
  call("PATCH", path, body);
export const apiPut = <T = any>(path: string, body: unknown): Promise<T> => call("PUT", path, body);
export const apiDelete = <T = any>(path: string, body?: unknown): Promise<T> =>
  call("DELETE", path, body);

/** A message's fields and files in one multipart request. */
export function apiUpload<T = any>(path: string, files: File[], fields: Record<string, string> = {}): Promise<T> {
  const form = new FormData();
  for (const [name, value] of Object.entries(fields)) form.append(name, value);
  for (const file of files) form.append("file", file, file.name);
  return call("POST", path, form);
}
