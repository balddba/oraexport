import { useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Mail, Plus, Settings } from "lucide-react";
import { api } from "../api";
import type { User } from "../types";
import { ErrorBox, Field, Modal } from "../components/ui";

export function SettingsPage() {
  const client = useQueryClient();
  const settings = useQuery({
    queryKey: ["notification-settings"],
    queryFn: () => api<Record<string, unknown>>("/notifications/settings"),
  });
  const deliveries = useQuery({
    queryKey: ["deliveries"],
    queryFn: () =>
      api<{
        items: {
          id: string;
          channel: string;
          state: string;
          attempts: number;
          error: string | null;
        }[];
      }>("/notifications/deliveries"),
    refetchInterval: 5000,
  });
  const users = useQuery({
    queryKey: ["users"],
    queryFn: () => api<User[]>("/users"),
  });
  const [error, setError] = useState<unknown>();
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [addingUser, setAddingUser] = useState(false);
  async function save(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const value = (key: string) => String(form.get(key) || "");
    const body: Record<string, unknown> = {
      smtp_host: value("smtp_host") || null,
      smtp_port: Number(value("smtp_port") || 587),
      smtp_starttls: form.get("smtp_starttls") === "on",
      smtp_username: value("smtp_username") || null,
      email_from: value("email_from") || null,
      email_to: value("email_to")
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
      webhook_url: value("webhook_url") || null,
    };
    for (const key of ["smtp_password", "webhook_token"])
      if (value(key)) body[key] = value(key);
    setBusy(true);
    try {
      await api("/notifications/settings", "PUT", body);
      client.invalidateQueries({ queryKey: ["notification-settings"] });
      setNotice("Notification settings saved.");
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  const config = settings.data || {};
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">WORKSPACE CONFIGURATION</span>
          <h1>Settings</h1>
          <p>Keep the right people informed and control workspace access.</p>
        </div>
      </div>
      <ErrorBox
        error={error || settings.error || deliveries.error || users.error}
      />
      {notice && (
        <div role="status" className="notice">
          {notice}
        </div>
      )}
      <section className="panel">
        <div className="panel-head">
          <div>
            <h2>
              <Mail size={18} /> Failure notifications
            </h2>
            <p>
              Email and HTTPS webhooks for exports, provisioning, and cleanup
              failures.
            </p>
          </div>
        </div>
        {settings.isSuccess && (
          <form
            className="panel-body"
            onSubmit={save}
            key={settings.dataUpdatedAt}
          >
            <div className="form-grid">
              <Field label="SMTP host">
                <input
                  name="smtp_host"
                  defaultValue={String(config.smtp_host || "")}
                />
              </Field>
              <Field label="SMTP port">
                <input
                  name="smtp_port"
                  type="number"
                  defaultValue={Number(config.smtp_port || 587)}
                />
              </Field>
              <Field label="SMTP username">
                <input
                  name="smtp_username"
                  defaultValue={String(config.smtp_username || "")}
                />
              </Field>
              <Field
                label="SMTP password"
                hint={
                  config.has_smtp_password
                    ? "Saved. Leave blank to preserve."
                    : "No password saved."
                }
              >
                <input
                  name="smtp_password"
                  type="password"
                  autoComplete="new-password"
                />
              </Field>
              <Field label="Sender email">
                <input
                  name="email_from"
                  type="email"
                  defaultValue={String(config.email_from || "")}
                />
              </Field>
              <Field
                label="Recipients"
                hint="Separate email addresses with commas"
              >
                <input
                  name="email_to"
                  defaultValue={
                    Array.isArray(config.email_to)
                      ? config.email_to.join(", ")
                      : ""
                  }
                />
              </Field>
            </div>
            <label className="checkbox">
              <input
                type="checkbox"
                name="smtp_starttls"
                defaultChecked={config.smtp_starttls !== false}
              />
              Use STARTTLS
            </label>
            <div className="form-grid">
              <Field label="Webhook URL">
                <input
                  name="webhook_url"
                  type="url"
                  placeholder="https://"
                  defaultValue={String(config.webhook_url || "")}
                />
              </Field>
              <Field
                label="Webhook bearer token"
                hint={
                  config.has_webhook_token
                    ? "Saved. Leave blank to preserve."
                    : "Optional authorization token"
                }
              >
                <input
                  name="webhook_token"
                  type="password"
                  autoComplete="new-password"
                />
              </Field>
            </div>
            <div className="settings-actions">
              <button className="button primary" disabled={busy}>
                {busy ? "Saving…" : "Save settings"}
              </button>
              <button
                className="button"
                type="button"
                onClick={async () => {
                  try {
                    await api("/notifications/test", "POST");
                    setNotice("Test notification queued.");
                  } catch (e) {
                    setError(e);
                  }
                }}
              >
                Send test notification
              </button>
            </div>
          </form>
        )}
      </section>
      <section className="panel">
        <div className="panel-head">
          <h2>Recent notification deliveries</h2>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Channel</th>
                <th>Status</th>
                <th>Attempts</th>
                <th>Error</th>
              </tr>
            </thead>
            <tbody>
              {deliveries.data?.items.map((item) => (
                <tr key={item.id}>
                  <td>{item.channel}</td>
                  <td>{item.state}</td>
                  <td>{item.attempts} / 5</td>
                  <td>{item.error || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <section className="panel">
        <div className="panel-head">
          <div>
            <h2>Workspace accounts</h2>
            <p>Admin, operator, and viewer access.</p>
          </div>
          <button className="button" onClick={() => setAddingUser(true)}>
            <Plus size={16} />
            Add account
          </button>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Username</th>
                <th>Role</th>
                <th>Status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {users.data?.map((user) => (
                <tr key={user.id}>
                  <td>{user.username}</td>
                  <td>{user.role}</td>
                  <td>{user.enabled ? "Enabled" : "Disabled"}</td>
                  <td>
                    {user.enabled && (
                      <button
                        className="text-button"
                        onClick={async () => {
                          try {
                            await api(`/users/${user.id}/disable`, "POST");
                            client.invalidateQueries({ queryKey: ["users"] });
                          } catch (e) {
                            setError(e);
                          }
                        }}
                      >
                        Disable
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      {addingUser && (
        <Modal title="Add workspace account" close={() => setAddingUser(false)}>
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              const form = new FormData(e.currentTarget);
              try {
                await api("/users", "POST", {
                  username: form.get("username"),
                  password: form.get("password"),
                  role: form.get("role"),
                });
                setAddingUser(false);
                client.invalidateQueries({ queryKey: ["users"] });
              } catch (e) {
                setError(e);
              }
            }}
          >
            <Field label="Username">
              <input required name="username" />
            </Field>
            <Field label="Password">
              <input
                required
                type="password"
                minLength={12}
                name="password"
                autoComplete="new-password"
              />
            </Field>
            <Field label="Role">
              <select name="role">
                <option value="viewer">Viewer</option>
                <option value="operator">Operator</option>
                <option value="admin">Admin</option>
              </select>
            </Field>
            <div className="modal-actions">
              <button className="button primary">Create account</button>
            </div>
          </form>
        </Modal>
      )}
    </>
  );
}
