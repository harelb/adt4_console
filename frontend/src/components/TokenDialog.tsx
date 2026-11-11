import {useEffect, useRef, useState, type FormEvent, type KeyboardEvent} from "react";
import {setToken, useAuthStatus} from "../lib/auth";
import {Modal} from "./ui";

const reload = () => window.location.reload();

export function TokenDialogHost({onAuthenticated = reload}: {onAuthenticated?: () => void}) {
  const required = useAuthStatus(s => s.required);
  return required ? <TokenDialog onAuthenticated={onAuthenticated} /> : null;
}

export function TokenDialog({onAuthenticated = reload}: {onAuthenticated?: () => void}) {
  const [token, updateToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!busy) input.current?.focus();
  }, [busy]);

  async function signIn(event: FormEvent) {
    event.preventDefault();
    const candidate = token.trim();
    if (busy || !candidate) return;
    setBusy(true);
    setError("");
    try {
      // Validate without saving a bad token or replaying the rejected request.
      const response = await fetch("api/health", {
        headers: {Authorization: `Bearer ${candidate}`},
        cache: "no-store",
      });
      if (response.status === 401) {
        setError("Incorrect token. Please try again.");
      } else if (!response.ok) {
        setError("Unable to sign in right now. Please try again.");
      } else {
        setToken(candidate);
        useAuthStatus.setState({required: false});
        // Start stores and sockets afresh with the verified token.
        onAuthenticated();
      }
    } catch {
      setError("Console server unreachable. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  function keepFocus(event: KeyboardEvent<HTMLFormElement>) {
    if (event.key !== "Tab") return;
    const controls = [...event.currentTarget.querySelectorAll<HTMLElement>("input:not(:disabled), button:not(:disabled)")];
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first?.focus();
    }
  }

  return <div className="token-dialog">
    <Modal title="Sign in to ADT4">
      <form onSubmit={signIn} onKeyDown={keepFocus}>
        <p>This console requires an access token.</p>
        <label className="field" htmlFor="console-access-token">
          <span className="field-label">Access token</span>
          <input ref={input} id="console-access-token" name="password" className="input"
            type="password" autoComplete="current-password" required disabled={busy}
            value={token} onChange={event => updateToken(event.target.value)}
            aria-invalid={!!error} aria-describedby={error ? "console-token-error" : "console-token-hint"} />
        </label>
        {error && <p id="console-token-error" className="tone-err" role="alert">{error}</p>}
        <p id="console-token-hint" className="field-hint">Your token is remembered in this browser.</p>
        <button className="btn btn-primary" type="submit" disabled={busy || !token.trim()}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </Modal>
  </div>;
}
