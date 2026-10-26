import { useState, type FormEvent } from "react";
import { api } from "../api";
import { ErrorBox, Field } from "../components/ui";

export function Account() {
  const [error, setError] = useState<unknown>();
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    setError(null);
    setNotice("");
    if (data.get("new_password") !== data.get("confirm_password")) {
      setError(new Error("New passwords do not match."));
      return;
    }
    setBusy(true);
    try {
      await api("/auth/password", "POST", {
        current_password: data.get("current_password"),
        new_password: data.get("new_password"),
      });
      form.reset();
      setNotice("Password changed. Your other sessions have been signed out.");
    } catch (error) {
      setError(error);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">YOUR ACCOUNT</span>
          <h1>Account</h1>
          <p>Manage your OraExport password.</p>
        </div>
      </div>
      <section className="panel">
        <div className="panel-head">
          <div>
            <h2>Change password</h2>
            <p>
              Your other sessions will be signed out. This browser will stay
              signed in.
            </p>
          </div>
        </div>
        <form className="panel-body" onSubmit={submit}>
          <ErrorBox error={error} />
          {notice && (
            <div className="notice" role="status">
              {notice}
            </div>
          )}
          <Field label="Current password">
            <input
              name="current_password"
              type="password"
              autoComplete="current-password"
              required
              disabled={busy}
            />
          </Field>
          <Field
            label="New password"
            hint="Use at least 8 characters and choose a different password."
          >
            <input
              name="new_password"
              type="password"
              autoComplete="new-password"
              minLength={8}
              required
              disabled={busy}
            />
          </Field>
          <Field label="Confirm new password">
            <input
              name="confirm_password"
              type="password"
              autoComplete="new-password"
              minLength={8}
              required
              disabled={busy}
            />
          </Field>
          <button className="button primary" disabled={busy}>
            {busy ? "Changing password…" : "Change password"}
          </button>
        </form>
      </section>
    </>
  );
}
