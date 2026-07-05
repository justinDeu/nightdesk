import { useState } from "react";
import type { FormEvent } from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { KeyRound } from "lucide-react";
import { login } from "@/api/auth";
import { Button } from "@/ui/Button";
import { Input, Field } from "@/ui/Input";

export function LoginPage() {
  const navigate = useNavigate();
  const search = useSearch({ from: "/login" }) as { redirect?: string };
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const ok = await login(token);
      if (ok) {
        navigate({ to: search.redirect ?? "/" });
      } else {
        setError("Bearer token does not match.");
      }
    } catch {
      setError("Could not reach the server. Is the API running?");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-ink-950 px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex flex-col items-center gap-3 text-center">
          <span className="dawn-edge grid h-11 w-11 place-items-center rounded-card text-ink-950">
            <span className="font-display text-lg font-bold">n</span>
          </span>
          <div>
            <h1 className="font-display text-xl font-semibold tracking-tight text-moon-100">
              nightdesk
            </h1>
            <p className="mt-1 text-sm text-moon-400">Sign in with your admin bearer token.</p>
          </div>
        </div>

        <form
          onSubmit={onSubmit}
          className="rounded-card border border-ink-700 bg-ink-900 p-5 shadow-[var(--shadow-panel)]"
        >
          <Field label="Bearer token" error={error ?? undefined} htmlFor="token">
            <Input
              id="token"
              type="password"
              autoFocus
              autoComplete="off"
              mono
              placeholder="paste token…"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              invalid={!!error}
            />
          </Field>
          <Button
            type="submit"
            variant="primary"
            className="mt-4 w-full"
            loading={busy}
            leadingIcon={<KeyRound size={15} />}
            disabled={!token.trim()}
          >
            Sign in
          </Button>
        </form>

        <p className="mt-4 text-center text-xs text-moon-600">
          The token is exchanged for a session cookie and never stored in the browser.
        </p>
      </div>
    </div>
  );
}
