import { ArrowRight, Box, Eye, EyeOff, ShieldCheck } from "lucide-react";
import { type FormEvent, useEffect, useRef, useState } from "react";
import StudioThemeToggle from "./studio-theme-toggle";
/* farm.css loaded by studio-dashboard.tsx */

function responseError(body: unknown): string | null {
  if (typeof body !== "object" || body === null) return null;
  const payload = body as Record<string, unknown>;
  const detail = payload.detail ?? payload.error ?? payload.message;
  if (typeof detail === "string") return detail.slice(0, 240);
  return null;
}

export default function StudioLogin() {
  const [showPassword, setShowPassword] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const errorRef = useRef<HTMLParagraphElement>(null);
  const pendingRef = useRef(false);

  useEffect(() => {
    if (error) errorRef.current?.focus();
  }, [error]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pendingRef.current) return;

    const form = event.currentTarget;
    const data = new FormData(form);
    const username = String(data.get("username") ?? "").trim();
    const password = String(data.get("password") ?? "");
    if (!username || !password) return;

    pendingRef.current = true;
    setSubmitting(true);
    setError("");
    try {
      const response = await fetch("/api/v1/dashboard/login", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (response.ok) {
        window.location.assign("/");
        return;
      }

      let detail: string | null = null;
      if (response.headers.get("content-type")?.includes("application/json")) {
        try {
          detail = responseError(await response.json());
        } catch {
          // A malformed response still gets a useful, generic error.
        }
      }
      setError(
        detail ||
          (response.status === 401
            ? "Those credentials didn't match. Try again."
            : "Sign-in failed. Please try again."),
      );
    } catch {
      setError("Can't reach the studio right now. Check your connection and try again.");
    } finally {
      pendingRef.current = false;
      setSubmitting(false);
    }
  }

  return (
    <main className="studio-login">
      <div className="login-theme-control">
        <StudioThemeToggle />
      </div>

      <section className="login-card" aria-labelledby="login-title">
        <header className="login-brand">
          <span className="login-brand-mark" aria-hidden="true">
            <Box size={17} strokeWidth={1.7} />
          </span>
          <span className="login-brand-copy">
            <strong>BLENDER MCP</strong>
            <small>STUDIO</small>
          </span>
        </header>

        <div className="login-head">
          <h1 id="login-title">Connect your coordinator</h1>
          <p className="login-subtitle">
            Sign in with your workspace credentials. They stay in this tab and are never written to disk.
          </p>
        </div>

        <form onSubmit={handleSubmit} className="login-form" noValidate={false}>
          <div className="login-field page-field">
            <label htmlFor="studio-username">Username</label>
            <input
              id="studio-username"
              className="page-input"
              name="username"
              type="text"
              autoComplete="username"
              placeholder="Your username"
              required
              disabled={submitting}
              aria-describedby={error ? "login-error" : undefined}
            />
          </div>

          <div className="login-field page-field">
            <label htmlFor="studio-password">Password</label>
            <div className="login-password-wrap">
              <input
                id="studio-password"
                className="page-input"
                name="password"
                type={showPassword ? "text" : "password"}
                autoComplete="current-password"
                placeholder="Your password"
                required
                disabled={submitting}
                aria-describedby={error ? "login-error" : undefined}
              />
              <button
                className="login-reveal"
                type="button"
                onClick={() => setShowPassword((value) => !value)}
                disabled={submitting}
                aria-label={showPassword ? "Hide password" : "Show password"}
                aria-pressed={showPassword}
              >
                {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
          </div>

          {error && (
            <p id="login-error" ref={errorRef} className="page-note is-bad login-error" role="alert" tabIndex={-1}>
              {error}
            </p>
          )}

          <button className="page-btn is-primary is-block login-submit" type="submit" disabled={submitting}>
            <span>{submitting ? "Signing in…" : "Sign in to Studio"}</span>
            {submitting ? (
              <span className="login-spinner" aria-hidden="true" />
            ) : (
              <ArrowRight size={16} aria-hidden="true" />
            )}
          </button>
        </form>

        <p className="login-form-note">
          <ShieldCheck size={14} aria-hidden="true" />
          <span>This session is scoped to your local coordinator and ends when the tab closes.</span>
        </p>
      </section>
    </main>
  );
}
