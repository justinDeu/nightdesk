import { api } from "./client";

/**
 * Login posts the bearer token to POST /auth/login (form-encoded).
 *
 * NOTE: the real endpoint field is `bearer` (see src/nightdesk/api/routes/auth.py
 * `login_submit(bearer: str = Form(...))`), not `token`. On success the server
 * sets the signed `nightdesk_session` cookie and 303-redirects to `/`; on a
 * mismatch it returns 401. We post with redirect: "manual" so the SPA — not the
 * browser — decides where to go, and we never store the token in JS.
 */
export async function login(token: string): Promise<boolean> {
  const form = new URLSearchParams();
  form.append("bearer", token.trim());

  const res = await fetch("/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: form.toString(),
    credentials: "same-origin",
    redirect: "manual",
  });

  // 303/redirect (opaqueredirect) or 2xx means the cookie was set.
  return res.ok || res.type === "opaqueredirect" || res.status === 303;
}

export async function logout(): Promise<void> {
  await api.post("/auth/logout").catch(() => undefined);
}
