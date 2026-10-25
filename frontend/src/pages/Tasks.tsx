import { useEffect, useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  Database,
  Play,
  Plus,
  Search,
  ChevronDown,
} from "lucide-react";
import { api, time, schemaLabel } from "../api";
import type {
  Catalog,
  Registration,
  Target,
  Task,
  TaskInput,
  User,
} from "../types";
import { defaultTask } from "../constants";
import { ErrorBox, Field, Modal, Empty, SearchBox } from "../components/ui";

export function Tasks({ user }: { user: User }) {
  const client = useQueryClient();
  const tasks = useQuery({
    queryKey: ["tasks"],
    queryFn: () => api<Task[]>("/tasks"),
    refetchInterval: 10000,
  });
  const targets = useQuery({
    queryKey: ["targets"],
    queryFn: () => api<Target[]>("/targets"),
  });
  const registrations = useQuery({
    queryKey: ["registrations"],
    queryFn: () => api<Registration[]>("/registrations"),
  });
  const [search, setSearch] = useState("");
  const [editing, setEditing] = useState<Task | "new">();
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState<string>();
  const [notice, setNotice] = useState("");
  const canEdit = user.role !== "viewer";
  async function run(task: Task) {
    setBusy(task.id);
    setError(null);
    try {
      await api(`/tasks/${task.id}/run`, "POST");
      setNotice(`${task.name} was queued.`);
      client.invalidateQueries({ queryKey: ["recent-runs"] });
    } catch (e) {
      setError(e);
    } finally {
      setBusy(undefined);
    }
  }
  const items = (tasks.data || []).filter((t) =>
    `${t.name} ${schemaLabel(t)}`.toLowerCase().includes(search.toLowerCase()),
  );
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">AUTOMATE YOUR EXPORTS</span>
          <h1>Export tasks</h1>
          <p>
            Select schemas or export a full target, with a schedule and
            retention policy.
          </p>
        </div>
        {canEdit && (
          <button className="button primary" onClick={() => setEditing("new")}>
            <Plus size={17} />
            New export task
          </button>
        )}
      </div>
      <ErrorBox
        error={error || tasks.error || targets.error || registrations.error}
      />
      {notice && (
        <div role="status" className="notice">
          {notice}
        </div>
      )}
      <section className="panel">
        <div className="toolbar">
          <SearchBox
            value={search}
            onChange={setSearch}
            placeholder="Search tasks or schemas"
          />
          <span>{items.length} tasks</span>
        </div>
        {items.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Task / schema</th>
                  <th>Database</th>
                  <th>Schedule</th>
                  <th>Retention</th>
                  <th>Policy</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {items.map((task) => (
                  <tr key={task.id}>
                    <td>
                      <strong>{task.name}</strong>
                      <small>{schemaLabel(task)}</small>
                    </td>
                    <td>
                      {targets.data?.find((t) => t.id === task.target_id)
                        ?.name || task.target_id}
                    </td>
                    <td>
                      <span className="mono">{task.cron}</span>
                      <small>
                        {task.timezone} ·{" "}
                        {task.enabled
                          ? `Next: ${time(task.next_run_at, task.timezone)}`
                          : "Paused"}
                      </small>
                    </td>
                    <td>
                      {task.retention_days} days
                      <small>Latest success preserved</small>
                    </td>
                    <td>
                      <span
                        className={`badge ${task.enabled ? "succeeded" : "cancelled"}`}
                      >
                        <span />
                        {task.enabled ? "Enabled" : "Paused"}
                      </span>
                    </td>
                    <td>
                      {canEdit && (
                        <div className="row-actions">
                          <button
                            className="button small"
                            disabled={busy === task.id}
                            onClick={() => run(task)}
                          >
                            <Play size={14} />
                            Run now
                          </button>
                          <button
                            className="text-button"
                            onClick={() => setEditing(task)}
                          >
                            Edit
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
            title="Create your first export task"
            description="Provision a database target, then choose schemas or a full export and schedule."
          />
        )}
      </section>
      {editing && (
        <TaskEditor
          task={editing === "new" ? undefined : editing}
          targets={targets.data || []}
          registrations={registrations.data || []}
          targetsLoading={targets.isPending || registrations.isPending}
          close={() => setEditing(undefined)}
          saved={() => {
            setEditing(undefined);
            client.invalidateQueries({ queryKey: ["tasks"] });
          }}
        />
      )}
    </>
  );
}

export function TaskEditor({
  task,
  targets,
  registrations = [],
  targetsLoading = false,
  close,
  saved,
}: {
  task?: Task;
  targets: Target[];
  registrations?: Registration[];
  targetsLoading?: boolean;
  close: () => void;
  saved: () => void;
}) {
  const initial = task
    ? (Object.fromEntries(
        Object.keys(defaultTask).map((k) => [k, task[k as keyof TaskInput]]),
      ) as unknown as TaskInput)
    : { ...defaultTask };
  initial.export_mode = task?.export_mode || "SCHEMA";
  initial.schema_names = task?.schema_names?.length
    ? [...task.schema_names]
    : task?.schema_name && initial.export_mode === "SCHEMA"
      ? [task.schema_name]
      : [];
  const [form, setForm] = useState<TaskInput>(initial);
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const [preset, setPreset] = useState("advanced");
  const [hour, setHour] = useState("02:00");
  const [weekday, setWeekday] = useState("1");
  const [preview, setPreview] = useState<{ utc: string; local: string }[]>();
  const catalog = useQuery({
    queryKey: ["catalog", form.target_id],
    queryFn: () => api<Catalog>(`/targets/${form.target_id}/catalog`),
    enabled: !!form.target_id,
  });
  const selectedTarget = targets.find((target) => target.id === form.target_id);
  const missingTargets = registrations.filter(
    (registration) =>
      !targets.some((target) => target.registration_id === registration.id),
  );
  useEffect(() => {
    if (!catalog.data) return;
    const { schemas, directories } = catalog.data;
    setForm((current) => ({
      ...current,
      schema_name:
        current.export_mode === "FULL"
          ? ""
          : current.schema_names[0] || (schemas.length === 1 ? schemas[0] : ""),
      schema_names:
        current.export_mode === "FULL"
          ? []
          : current.schema_names.length
            ? current.schema_names
            : schemas.length === 1
              ? [schemas[0]]
              : [],
      directory:
        current.directory ||
        directories.find(
          (directory) => directory.name === selectedTarget?.directory,
        )?.name ||
        (directories.length === 1 ? directories[0].name : ""),
    }));
  }, [
    catalog.data,
    selectedTarget?.directory,
    form.target_id,
    form.export_mode,
  ]);
  function update<K extends keyof TaskInput>(key: K, value: TaskInput[K]) {
    setForm({ ...form, [key]: value });
    setPreview(undefined);
  }
  function schedule(mode: string, clock: string, day: string) {
    setPreset(mode);
    setHour(clock);
    setWeekday(day);
    if (mode !== "advanced") {
      const [h, m] = clock.split(":").map(Number);
      update("cron", `${m} ${h} * * ${mode === "weekly" ? day : "*"}`);
    }
  }
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (
      !selectedTarget?.provisioned ||
      !catalog.data ||
      catalog.isFetching ||
      catalog.error ||
      (form.export_mode === "SCHEMA" &&
        (!form.schema_names.length ||
          form.schema_names.some(
            (name) => !catalog.data.schemas.includes(name),
          )))
    )
      return;
    setBusy(true);
    setError(null);
    try {
      await api(
        task ? `/tasks/${task.id}` : "/tasks",
        task ? "PUT" : "POST",
        form,
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
      title={task ? "Edit export task" : "New export task"}
      subtitle="Configure the export and its next scheduled run."
      close={close}
    >
      <form onSubmit={submit}>
        <ErrorBox error={error || catalog.error} />
        <div className="form-grid">
          <Field label="Task name">
            <input
              required
              value={form.name}
              onChange={(e) => update("name", e.target.value)}
              placeholder="Finance nightly export"
            />
          </Field>
          <Field
            label="Database target"
            hint={
              missingTargets.length
                ? "Registrations marked ‘add export target’ need a direct PDB or non-CDB target in Databases."
                : undefined
            }
          >
            <select
              required
              value={form.target_id}
              aria-label="Database target"
              disabled={targetsLoading}
              onChange={(e) => {
                setPreview(undefined);
                setForm({
                  ...form,
                  target_id: e.target.value,
                  schema_name: "",
                  schema_names: [],
                  directory: "",
                });
              }}
            >
              <option value="">
                {targetsLoading
                  ? "Loading databases…"
                  : "Select a database target"}
              </option>
              {targets.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                  {t.provisioned ? "" : " · needs provisioning"}
                </option>
              ))}
              {missingTargets.map((registration) => (
                <option
                  key={`registration-${registration.id}`}
                  value={`registration-${registration.id}`}
                  disabled
                >
                  {registration.name} · add export target
                </option>
              ))}
            </select>
          </Field>
          {selectedTarget && !selectedTarget.provisioned && (
            <p role="status" className="muted">
              Provision this target’s export account in Databases before saving
              an export task.
            </p>
          )}
          <Field label="Export scope">
            <select
              value={form.export_mode}
              onChange={(e) => {
                setForm({
                  ...form,
                  export_mode: e.target.value as TaskInput["export_mode"],
                  schema_name: "",
                  schema_names: [],
                });
              }}
            >
              <option value="SCHEMA">Selected schemas</option>
              <option value="FULL">Full export</option>
            </select>
          </Field>
          {form.export_mode === "SCHEMA" ? (
            <SchemaSelect
              key={form.target_id}
              available={catalog.data?.schemas || []}
              selected={form.schema_names}
              disabled={!form.target_id || catalog.isFetching || !catalog.data}
              onChange={(selected) =>
                setForm({
                  ...form,
                  schema_names: selected,
                  schema_name: selected[0] || "",
                })
              }
            />
          ) : (
            <p className="form-hint">
              Full export includes all exportable data and metadata in the
              selected PDB or non-CDB, subject to the Content option.
            </p>
          )}
          <Field label="Oracle DIRECTORY">
            <select
              required
              value={form.directory}
              disabled={!form.target_id || catalog.isFetching || !catalog.data}
              onChange={(e) => update("directory", e.target.value)}
            >
              <option value="">
                {catalog.isFetching
                  ? "Loading directories…"
                  : "Select a directory"}
              </option>
              {catalog.data?.directories.map((d) => (
                <option key={d.name} value={d.name}>
                  {d.name} · {d.path}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <h3 className="form-section">Schedule</h3>
        <div className="form-grid">
          <Field label="Schedule type">
            <select
              value={preset}
              onChange={(e) => schedule(e.target.value, hour, weekday)}
            >
              <option value="advanced">Advanced cron</option>
              <option value="daily">Daily</option>
              <option value="weekly">Weekly</option>
            </select>
          </Field>
          {preset !== "advanced" && (
            <Field label="Local time">
              <input
                type="time"
                value={hour}
                onChange={(e) => schedule(preset, e.target.value, weekday)}
              />
            </Field>
          )}
          {preset === "weekly" && (
            <Field label="Day of week">
              <select
                value={weekday}
                onChange={(e) => schedule(preset, hour, e.target.value)}
              >
                {[
                  "Sunday",
                  "Monday",
                  "Tuesday",
                  "Wednesday",
                  "Thursday",
                  "Friday",
                  "Saturday",
                ].map((day, i) => (
                  <option key={day} value={i}>
                    {day}
                  </option>
                ))}
              </select>
            </Field>
          )}
          <Field
            label="Cron expression"
            hint="Five fields: minute hour day month weekday"
          >
            <input
              required
              value={form.cron}
              onChange={(e) => update("cron", e.target.value)}
            />
          </Field>
          <Field label="Timezone">
            <input
              required
              value={form.timezone}
              onChange={(e) => update("timezone", e.target.value)}
            />
          </Field>
        </div>
        <button
          className="text-button"
          type="button"
          onClick={async () => {
            try {
              setPreview(
                await api("/schedules/preview", "POST", {
                  cron: form.cron,
                  timezone: form.timezone,
                }),
              );
              setError(null);
            } catch (e) {
              setError(e);
            }
          }}
        >
          Preview next five runs <ArrowRight size={15} />
        </button>
        {preview && (
          <div className="schedule-preview">
            {preview.map((item) => (
              <div key={item.utc}>{item.local}</div>
            ))}
          </div>
        )}
        <h3 className="form-section">Export options</h3>
        <div className="form-grid">
          <Field label="Parallel workers">
            <input
              type="number"
              min="1"
              max="64"
              value={form.parallel}
              onChange={(e) => update("parallel", Number(e.target.value))}
            />
          </Field>
          <Field label="Maximum file size (GB)">
            <input
              type="number"
              min="1"
              max="16384"
              value={form.file_size_gb}
              onChange={(e) => update("file_size_gb", Number(e.target.value))}
            />
          </Field>
          <Field label="Content">
            <select
              value={form.content}
              onChange={(e) =>
                update("content", e.target.value as TaskInput["content"])
              }
            >
              <option value="ALL">Data and metadata</option>
              <option value="DATA_ONLY">Data only</option>
              <option value="METADATA_ONLY">Metadata only</option>
            </select>
          </Field>
          <Field
            label="Retention (days)"
            hint="The latest successful export is always preserved."
          >
            <input
              type="number"
              min="1"
              value={form.retention_days}
              onChange={(e) => update("retention_days", Number(e.target.value))}
            />
          </Field>
        </div>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={form.consistent_snapshot}
            onChange={(e) => update("consistent_snapshot", e.target.checked)}
          />
          Use a consistent snapshot at export startup
        </label>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={(e) => update("enabled", e.target.checked)}
          />
          Enable scheduled exports
        </label>
        <div className="modal-actions">
          <button type="button" className="button" onClick={close}>
            Cancel
          </button>
          <button
            className="button primary"
            disabled={
              busy ||
              !selectedTarget?.provisioned ||
              !catalog.data ||
              catalog.isFetching ||
              !!catalog.error ||
              (form.export_mode === "SCHEMA" &&
                (!form.schema_names.length ||
                  form.schema_names.some(
                    (name) => !catalog.data?.schemas.includes(name),
                  )))
            }
          >
            {busy ? "Verifying & saving…" : "Save task"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function SchemaSelect({
  available,
  selected,
  disabled,
  onChange,
}: {
  available: string[];
  selected: string[];
  disabled: boolean;
  onChange: (schemas: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const root = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const options = [...new Set([...available, ...selected])].filter((name) =>
    name.toLowerCase().includes(search.toLowerCase()),
  );
  useEffect(() => {
    function outside(event: PointerEvent) {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, []);
  return (
    <div
      className="field schema-picker"
      ref={root}
      onKeyDown={(event) => {
        if (event.key === "Escape" && open) {
          event.stopPropagation();
          setOpen(false);
          button.current?.focus();
        }
      }}
    >
      <span>Schemas</span>
      <button
        ref={button}
        type="button"
        className="schema-trigger"
        aria-label="Schemas"
        aria-expanded={open && !disabled}
        aria-controls="schema-options"
        disabled={disabled}
        onClick={() => setOpen(!open)}
      >
        <span>
          {selected.length
            ? selected.join(", ")
            : disabled
              ? "Choose a target to load schemas"
              : "Select schemas"}
        </span>
        <ChevronDown size={16} />
      </button>
      {open && !disabled && (
        <div className="schema-menu" id="schema-options">
          <input
            autoFocus
            aria-label="Search schemas"
            placeholder="Search schemas…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <div className="schema-controls">
            <button
              type="button"
              className="text-button"
              onClick={() =>
                onChange(
                  [
                    ...new Set([
                      ...selected,
                      ...options.filter((name) => available.includes(name)),
                    ]),
                  ].slice(0, 200),
                )
              }
            >
              Select matching
            </button>
            <button
              type="button"
              className="text-button"
              onClick={() => onChange([])}
            >
              Clear selection
            </button>
          </div>
          <div
            className="schema-options"
            role="group"
            aria-label="Available schemas"
          >
            {options.map((name) => (
              <label className="checkbox" key={name}>
                <input
                  type="checkbox"
                  checked={selected.includes(name)}
                  disabled={selected.length >= 200 && !selected.includes(name)}
                  onChange={(e) =>
                    onChange(
                      e.target.checked
                        ? [...selected, name]
                        : selected.filter((item) => item !== name),
                    )
                  }
                />
                {name}
                {!available.includes(name) && " (unavailable)"}
              </label>
            ))}
            {!options.length && (
              <p className="muted">No schemas match your search.</p>
            )}
          </div>
          <button
            type="button"
            className="button small"
            onClick={() => {
              setOpen(false);
              button.current?.focus();
            }}
          >
            Done
          </button>
        </div>
      )}
      <small>{selected.length} selected · select up to 200 schemas</small>
      {selected.some((name) => !available.includes(name)) && !disabled && (
        <small role="alert">Remove unavailable schemas before saving.</small>
      )}
    </div>
  );
}
