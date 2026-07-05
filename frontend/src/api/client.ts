/**
 * Thin typed fetch wrapper for the nightdesk JSON API.
 *
 * - Always same-origin (the vite dev server proxies /api + /auth; in prod the
 *   SPA is served by FastAPI on the same origin), cookies included.
 * - JSON in / JSON out. Errors are normalized to `ApiError`.
 * - A 401 anywhere dispatches a global `nightdesk:unauthorized` event; the
 *   router listens and redirects to /login. The wrapper never stores tokens.
 */

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, message: string, detail: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export const UNAUTHORIZED_EVENT = "nightdesk:unauthorized";

function emitUnauthorized() {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
  }
}

type Query = Record<string, unknown>;

function buildUrl(path: string, query?: Query): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined) continue;
    params.append(key, String(value));
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

export interface RequestOptions {
  query?: Query;
  body?: unknown;
  signal?: AbortSignal;
  /** Form-encode the body instead of JSON (used by /auth/login). */
  form?: boolean;
}

async function request<T>(
  method: string,
  path: string,
  opts: RequestOptions = {},
): Promise<T> {
  const headers: Record<string, string> = {};
  let body: BodyInit | undefined;

  if (opts.body !== undefined) {
    if (opts.form) {
      headers["Content-Type"] = "application/x-www-form-urlencoded";
      const form = new URLSearchParams();
      for (const [k, v] of Object.entries(opts.body as Record<string, string>)) {
        form.append(k, v);
      }
      body = form.toString();
    } else {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.body);
    }
  }

  let res: Response;
  try {
    res = await fetch(buildUrl(path, opts.query), {
      method,
      headers,
      body,
      credentials: "same-origin",
      signal: opts.signal,
    });
  } catch (err) {
    if ((err as Error).name === "AbortError") throw err;
    throw new ApiError(0, "Network request failed", err);
  }

  if (res.status === 401) {
    emitUnauthorized();
    throw new ApiError(401, "Unauthorized", null);
  }

  if (res.status === 204) {
    return undefined as T;
  }

  const contentType = res.headers.get("content-type") ?? "";
  const isJson = contentType.includes("application/json");
  const payload = isJson ? await res.json().catch(() => null) : await res.text();

  if (!res.ok) {
    const message =
      (isJson && payload && typeof payload === "object" && "detail" in payload
        ? stringifyDetail((payload as { detail: unknown }).detail)
        : null) ?? `Request failed (${res.status})`;
    throw new ApiError(res.status, message, payload);
  }

  return payload as T;
}

/**
 * Like `request` but also returns the raw response headers, for endpoints whose
 * pagination metadata rides in headers (e.g. `X-Total-Count` on GET /tickets).
 * GET-only and additive — the plain `api.*` helpers are unchanged.
 */
async function getWithMeta<T>(
  path: string,
  opts: RequestOptions = {},
): Promise<{ data: T; headers: Headers }> {
  let res: Response;
  try {
    res = await fetch(buildUrl(path, opts.query), {
      method: "GET",
      credentials: "same-origin",
      signal: opts.signal,
    });
  } catch (err) {
    if ((err as Error).name === "AbortError") throw err;
    throw new ApiError(0, "Network request failed", err);
  }

  if (res.status === 401) {
    emitUnauthorized();
    throw new ApiError(401, "Unauthorized", null);
  }

  const contentType = res.headers.get("content-type") ?? "";
  const isJson = contentType.includes("application/json");
  const payload = isJson ? await res.json().catch(() => null) : await res.text();

  if (!res.ok) {
    const message =
      (isJson && payload && typeof payload === "object" && "detail" in payload
        ? stringifyDetail((payload as { detail: unknown }).detail)
        : null) ?? `Request failed (${res.status})`;
    throw new ApiError(res.status, message, payload);
  }

  return { data: payload as T, headers: res.headers };
}

function stringifyDetail(detail: unknown): string | null {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    // FastAPI validation errors: [{loc, msg, type}, ...]
    return detail
      .map((d) =>
        d && typeof d === "object" && "msg" in d ? String((d as { msg: unknown }).msg) : String(d),
      )
      .join("; ");
  }
  return null;
}

export const api = {
  get: <T>(path: string, opts?: RequestOptions) => request<T>("GET", path, opts),
  getWithMeta,
  post: <T>(path: string, opts?: RequestOptions) => request<T>("POST", path, opts),
  patch: <T>(path: string, opts?: RequestOptions) => request<T>("PATCH", path, opts),
  put: <T>(path: string, opts?: RequestOptions) => request<T>("PUT", path, opts),
  delete: <T>(path: string, opts?: RequestOptions) => request<T>("DELETE", path, opts),
};
