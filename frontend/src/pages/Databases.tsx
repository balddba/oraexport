import { useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Database,
  Plus,
  Pencil,
  RefreshCw,
  ShieldCheck,
  Trash2,
  X,
} from "lucide-react";
import { api } from "../api";
import type {
  Catalog,
  Registration,
  Target,
  User,
  Discovery,
  TargetPrefill,
} from "../types";
import { Badge, ErrorBox, Field, Modal, Empty } from "../components/ui";

export function Databases({
  user,
  onExports,
}: {
  user: User;
  onExports?: (targetId: string) => void;
}) {
  const client = useQueryClient();
  const registrations = useQuery({
    queryKey: ["registrations"],
    queryFn: () => api<Registration[]>("/registrations"),
  });
  const targets = useQuery({
    queryKey: ["targets"],
    queryFn: () => api<Target[]>("/targets"),
  });
  const [modal, setModal] = useState<"registration" | "target">();
  const [targetPrefill, setTargetPrefill] = useState<TargetPrefill>();
  const [editing, setEditing] = useState<Registration>();
  const [provision, setProvision] = useState<Target>();
  const [error, setError] = useState<unknown>();
  const [removal, setRemoval] = useState<{
    kind: "registrations" | "targets";
    id: string;
    name: string;
  }>();
  const [removalError, setRemovalError] = useState<unknown>();
  const [discovery, setDiscovery] = useState<{
    id: string;
    name: string;
    data: Discovery;
  }>();
  const [busy, setBusy] = useState<string>();
  const admin = user.role === "admin";
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">YOUR ORACLE ESTATE</span>
          <h1>Databases</h1>
          <p>
            Register your databases, connect PDBs, and provision export
            accounts.
          </p>
        </div>
        {admin && (
          <button
            className="button primary"
            onClick={() => setModal("registration")}
          >
            <Plus size={17} />
            Register database
          </button>
        )}
      </div>
      <ErrorBox error={error || registrations.error || targets.error} />
      <section className="panel">
        <div className="panel-head">
          <div>
            <h2>Database registrations</h2>
            <p>
              CDB roots discover PDBs. Exports run through direct target
              services.
            </p>
          </div>
        </div>
        {registrations.data?.length ? (
          <div className="database-grid">
            {registrations.data.map((reg) => (
              <div className="database-card" key={reg.id}>
                <div className="database-card-top">
                  <span className="target-icon">
                    <Database size={22} />
                  </span>
                  <span className="subtle-chip">
                    {reg.kind.replace("_", "-").toUpperCase()}
                  </span>
                </div>
                <h3>{reg.name}</h3>
                <p className="mono">{reg.dsn}</p>
                <small>
                  {reg.version
                    ? `Oracle ${reg.version}`
                    : "Connection not yet tested"}
                </small>
                {admin && (
                  <div className="row-actions registration-actions">
                    <button
                      className="button small"
                      disabled={busy === reg.id}
                      aria-label={`Edit database ${reg.name}`}
                      onClick={() => setEditing(reg)}
                    >
                      <Pencil size={14} /> Edit
                    </button>
                    <button
                      className="button small"
                      disabled={busy === reg.id}
                      onClick={async () => {
                        setBusy(reg.id);
                        try {
                          const data = await api<Discovery>(
                            `/registrations/${reg.id}/discover`,
                            "POST",
                          );
                          setDiscovery({ id: reg.id, name: reg.name, data });
                          client.invalidateQueries({
                            queryKey: ["registrations"],
                          });
                          setError(null);
                        } catch (e) {
                          setError(e);
                        } finally {
                          setBusy(undefined);
                        }
                      }}
                    >
                      <RefreshCw size={14} />
                      Test & discover
                    </button>
                    <button
                      className="button small danger"
                      disabled={busy === reg.id}
                      aria-label={`Remove database ${reg.name}`}
                      onClick={() => {
                        setRemovalError(undefined);
                        setRemoval({
                          kind: "registrations",
                          id: reg.id,
                          name: reg.name,
                        });
                      }}
                    >
                      <Trash2 size={14} /> Remove
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        ) : (
          <Empty
            title="Connect your Oracle estate"
            description="Register a CDB, PDB, or non-container database to get started."
          />
        )}
      </section>
      {discovery && (
        <section className="panel">
          <div className="panel-head">
            <h2>Discovery · {discovery.name}</h2>
            <button
              className="icon-button"
              aria-label="Close discovery"
              onClick={() => setDiscovery(undefined)}
            >
              <X size={18} />
            </button>
          </div>
          <div className="panel-body">
            <p>
              Connected successfully · {discovery.data.kind} · Oracle{" "}
              {discovery.data.version}
            </p>
            {discovery.data.pdbs.map((p) => (
              <div className="discovery-row" key={p.name}>
                <strong>{p.name}</strong>
                <span>{p.open_mode}</span>
                {admin && (
                  <button
                    className="button small"
                    aria-label={`Add target for ${p.name}`}
                    onClick={() => {
                      setTargetPrefill({
                        registrationId: discovery.id,
                        name: `${discovery.name} · ${p.name}`,
                        dsn:
                          p.services?.find((service) => service.dsn)?.dsn || "",
                        pdb: p,
                      });
                      setModal("target");
                    }}
                  >
                    <Plus size={14} /> Add target
                  </button>
                )}
              </div>
            ))}
            {discovery.data.pdbs.length > 0 && (
              <p className="muted">
                Select Add target beside a PDB to prefill its connection
                details. You can review the service DSN before saving.
              </p>
            )}
          </div>
        </section>
      )}
      <section className="panel">
        <div className="panel-head">
          <div>
            <h2>Export targets</h2>
            <p>A local export account for each selected PDB or non-CDB.</p>
          </div>
          {admin && (
            <button
              className="button"
              onClick={() => {
                setTargetPrefill(undefined);
                setModal("target");
              }}
            >
              <Plus size={16} />
              Add target
            </button>
          )}
        </div>
        {targets.data?.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Target</th>
                  <th>Service DSN</th>
                  <th>Directory</th>
                  <th>Provisioning</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {targets.data.map((target) => (
                  <tr key={target.id}>
                    <td>
                      <strong>{target.name}</strong>
                      <small>{target.container_name || "Non-CDB"}</small>
                    </td>
                    <td className="mono">
                      <span className="service-dsn" title={target.dsn}>
                        {target.dsn}
                      </span>
                    </td>
                    <td>{target.directory || "Not configured"}</td>
                    <td>
                      <Badge
                        state={target.provisioned ? "succeeded" : "queued"}
                      />
                    </td>
                    <td>
                      {onExports && (
                        <button
                          className="button small"
                          onClick={() => onExports(target.id)}
                        >
                          Manage exports
                        </button>
                      )}
                      {admin && (
                        <div className="row-actions">
                          <button
                            className="button small"
                            onClick={() => setProvision(target)}
                          >
                            <ShieldCheck size={14} />
                            {target.provisioned
                              ? "Review account"
                              : "Provision"}
                          </button>
                          <button
                            className="button small danger"
                            aria-label={`Remove target ${target.name}`}
                            onClick={() => {
                              setRemovalError(undefined);
                              setRemoval({
                                kind: "targets",
                                id: target.id,
                                name: target.name,
                              });
                            }}
                          >
                            <Trash2 size={14} /> Remove
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty
            title="No export targets yet"
            description="Add a direct service connection, then provision its export account."
          />
        )}
      </section>
      {removal && (
        <Modal
          title={`Remove ${removal.name}?`}
          subtitle="This removes the saved connection from OraExport. Oracle databases, accounts, and export files are unchanged."
          close={() => {
            if (!busy) setRemoval(undefined);
          }}
        >
          <ErrorBox error={removalError} />
          <p>
            {removal.kind === "registrations"
              ? "Remove this database's export targets first."
              : "Targets with export tasks or run history cannot be removed."}
          </p>
          <div className="modal-actions">
            <button
              className="button"
              disabled={!!busy}
              onClick={() => setRemoval(undefined)}
            >
              Cancel
            </button>
            <button
              className="button danger"
              disabled={!!busy}
              onClick={async () => {
                setBusy(removal.id);
                try {
                  await api(`/${removal.kind}/${removal.id}`, "DELETE");
                  if (discovery?.id === removal.id) setDiscovery(undefined);
                  setRemoval(undefined);
                  setError(undefined);
                  await Promise.all([
                    client.invalidateQueries({ queryKey: ["registrations"] }),
                    client.invalidateQueries({ queryKey: ["targets"] }),
                    client.invalidateQueries({ queryKey: ["summary"] }),
                  ]);
                } catch (e) {
                  setRemovalError(e);
                } finally {
                  setBusy(undefined);
                }
              }}
            >
              {busy ? "Removing…" : "Remove connection"}
            </button>
          </div>
        </Modal>
      )}
      {modal && (
        <ConnectionForm
          kind={modal}
          targetPrefill={targetPrefill}
          registrations={registrations.data || []}
          close={() => setModal(undefined)}
          saved={() => {
            setModal(undefined);
            client.invalidateQueries({
              queryKey: [modal === "target" ? "targets" : "registrations"],
            });
          }}
        />
      )}
      {editing && (
        <ConnectionForm
          kind="registration"
          registration={editing}
          registrations={registrations.data || []}
          close={() => setEditing(undefined)}
          saved={() => {
            setEditing(undefined);
            setDiscovery(undefined);
            client.invalidateQueries({ queryKey: ["registrations"] });
            client.invalidateQueries({ queryKey: ["targets"] });
          }}
        />
      )}
      {provision && (
        <ProvisionForm
          target={provision}
          close={() => setProvision(undefined)}
          saved={() => {
            setProvision(undefined);
            client.invalidateQueries({ queryKey: ["targets"] });
          }}
        />
      )}
    </>
  );
}

export function ConnectionForm({
  kind,
  registration,
  targetPrefill,
  registrations,
  close,
  saved,
}: {
  kind: "registration" | "target";
  registration?: Registration;
  targetPrefill?: TargetPrefill;
  registrations: Registration[];
  close: () => void;
  saved: () => void;
}) {
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const [dsn, setDsn] = useState(registration?.dsn || targetPrefill?.dsn || "");
  const [ownCredentials, setOwnCredentials] = useState(
    kind === "registration" && !registration,
  );
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const data = new FormData(e.currentTarget);
    const body: Record<string, unknown> = {
      name: data.get("name"),
      dsn: data.get("dsn"),
    };
    if (kind === "target") {
      body.registration_id = data.get("registration_id");
      if (targetPrefill?.pdb) body.pdb_name = targetPrefill.pdb.name;
    }
    if (ownCredentials) {
      body.username = data.get("username");
      body.password = data.get("password");
      body.sysdba = data.get("sysdba") === "on";
    }
    setBusy(true);
    try {
      await api(
        registration
          ? `/registrations/${registration.id}`
          : kind === "target"
            ? "/targets"
            : "/registrations",
        registration ? "PUT" : "POST",
        body,
      );
      saved();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title={
        registration
          ? "Edit database registration"
          : kind === "target"
            ? "Add export target"
            : "Register database"
      }
      subtitle="Credentials are encrypted before storage."
      close={close}
    >
      <form onSubmit={submit}>
        <ErrorBox error={error} />
        {kind === "target" && (
          <Field label="Database registration">
            <select
              required
              name="registration_id"
              defaultValue={targetPrefill?.registrationId || ""}
            >
              <option value="">Select a registration</option>
              {registrations
                .filter(
                  (reg) =>
                    !targetPrefill?.pdb ||
                    reg.id === targetPrefill.registrationId,
                )
                .map((reg) => (
                  <option key={reg.id} value={reg.id}>
                    {reg.name}
                  </option>
                ))}
            </select>
          </Field>
        )}
        <Field label="Display name">
          <input
            required
            name="name"
            defaultValue={registration?.name || targetPrefill?.name}
            placeholder={
              kind === "target" ? "Production · Finance PDB" : "Production CDB"
            }
          />
        </Field>
        {targetPrefill?.pdb && (
          <>
            <Field
              label="Discovered service"
              hint="The suggested DSN uses this registration’s listener. Review it if the PDB uses a different listener."
            >
              <select
                defaultValue={
                  targetPrefill.pdb.services?.find((service) => service.dsn)
                    ?.dsn || ""
                }
                onChange={(event) => setDsn(event.target.value)}
              >
                <option value="">Enter a service DSN manually</option>
                {(targetPrefill.pdb.services || [])
                  .filter((service) => service.dsn)
                  .map((service) => (
                    <option key={service.name} value={service.dsn!}>
                      {service.name}
                    </option>
                  ))}
              </select>
            </Field>
            {!targetPrefill.dsn && (
              <p className="form-hint">
                No service DSN could be suggested for this PDB. Enter its direct
                service DSN below.
              </p>
            )}
          </>
        )}
        <Field
          label="Service DSN"
          hint="host:port/service_name — targets must connect directly to a PDB or non-CDB"
        >
          <input
            required
            name="dsn"
            value={dsn}
            onChange={(event) => setDsn(event.target.value)}
            placeholder="db.example.com:1521/FINANCE"
          />
        </Field>
        {(kind === "target" || registration) && (
          <label className="checkbox">
            <input
              type="checkbox"
              checked={ownCredentials}
              onChange={(e) => setOwnCredentials(e.target.checked)}
            />
            {registration
              ? "Replace DBA credentials (otherwise keep saved credentials)"
              : "Use separate DBA credentials (otherwise inherit registration)"}
          </label>
        )}
        {registration && (
          <p className="form-hint">
            After changing the DSN or credentials, use Test & discover again.
            Export target services stay the same. Replacing credentials also
            updates targets that inherit this registration’s DBA credentials.
          </p>
        )}
        {ownCredentials && (
          <>
            <div className="form-grid">
              <Field label="DBA username">
                <input required name="username" autoComplete="off" />
              </Field>
              <Field label="DBA password">
                <input
                  required
                  name="password"
                  type="password"
                  autoComplete="new-password"
                />
              </Field>
            </div>
            <label className="checkbox">
              <input type="checkbox" name="sysdba" />
              Connect as SYSDBA
            </label>
          </>
        )}
        <div className="modal-actions">
          <button type="button" className="button" onClick={close}>
            Cancel
          </button>
          <button className="button primary" disabled={busy}>
            {busy
              ? registration
                ? "Saving…"
                : "Connecting…"
              : registration
                ? "Save changes"
                : kind === "target"
                  ? "Verify & add target"
                  : "Save registration"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function ProvisionForm({
  target,
  close,
  saved,
}: {
  target: Target;
  close: () => void;
  saved: () => void;
}) {
  const catalog = useQuery({
    queryKey: ["catalog", target.id],
    queryFn: () => api<Catalog>(`/targets/${target.id}/catalog`),
  });
  const [form, setForm] = useState({
    export_username: target.export_username || "ORAEXPORT",
    directory: target.directory || "",
    tablespace: target.tablespace,
    quota_mb: 100,
    consistent_snapshot: false,
  });
  const [preview, setPreview] = useState<{ sql: string[]; note: string }>();
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  function change(key: string, value: unknown) {
    setForm({ ...form, [key]: value });
    setPreview(undefined);
  }
  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      if (preview) {
        await api(`/targets/${target.id}/provision/apply`, "POST", form);
        saved();
      } else
        setPreview(
          await api(`/targets/${target.id}/provision/preview`, "POST", form),
        );
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title={`Provision · ${target.name}`}
      subtitle="Review the SQL before creating or updating the dedicated export account."
      close={close}
    >
      <form onSubmit={submit}>
        <ErrorBox error={error || catalog.error} />
        <div className="form-grid">
          <Field label="Export username">
            <input
              required
              value={form.export_username}
              onChange={(e) => change("export_username", e.target.value)}
            />
          </Field>
          <Field label="Oracle DIRECTORY">
            <select
              required
              value={form.directory}
              onChange={(e) => change("directory", e.target.value)}
            >
              <option value="">Select a filesystem directory</option>
              {catalog.data?.directories.map((d) => (
                <option key={d.name} value={d.name}>
                  {d.name} · {d.path}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Master table tablespace">
            <select
              value={form.tablespace}
              onChange={(e) => change("tablespace", e.target.value)}
            >
              {catalog.data?.tablespaces.map((t) => (
                <option key={t}>{t}</option>
              ))}
            </select>
          </Field>
          <Field label="Quota (MB)">
            <input
              type="number"
              min="10"
              required
              value={form.quota_mb}
              onChange={(e) => change("quota_mb", Number(e.target.value))}
            />
          </Field>
        </div>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={form.consistent_snapshot}
            onChange={(e) => change("consistent_snapshot", e.target.checked)}
          />
          Grant privileges for consistent snapshots
        </label>
        {preview && (
          <>
            <pre className="sql-preview">{preview.sql.join("\n\n")}</pre>
            <p className="muted">{preview.note}</p>
          </>
        )}
        <div className="modal-actions">
          <button type="button" className="button" onClick={close}>
            Cancel
          </button>
          <button className="button primary" disabled={busy}>
            {busy ? "Working…" : preview ? "Apply provisioning" : "Preview SQL"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
