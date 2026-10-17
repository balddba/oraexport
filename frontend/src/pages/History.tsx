import { schemaLabel } from "../api";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowDownToLine,
  ChevronLeft,
  ChevronRight,
  Database,
  HardDrive,
  Search,
} from "lucide-react";
import { api, bytes, time } from "../api";
import type { Page, Run, Target, Task, User } from "../types";
import { activeStates, labels } from "../constants";
import {
  Badge,
  ErrorBox,
  Field,
  Modal,
  RunTable,
  SearchBox,
} from "../components/ui";

export function History({ user }: { user: User }) {
  const [state, setState] = useState("");
  const [taskId, setTaskId] = useState("");
  const [targetId, setTargetId] = useState("");
  const [page, setPage] = useState(1);
  const [detail, setDetail] = useState<string>();
  const [search, setSearch] = useState("");
  const tasks = useQuery({
    queryKey: ["tasks"],
    queryFn: () => api<Task[]>("/tasks"),
  });
  const targets = useQuery({
    queryKey: ["targets"],
    queryFn: () => api<Target[]>("/targets"),
  });
  const filters = new URLSearchParams({
    state,
    task_id: taskId,
    target_id: targetId,
  });
  for (const [key, value] of [...filters.entries()])
    if (!value) filters.delete(key);
  const runs = useQuery({
    queryKey: ["history", state, taskId, targetId, page],
    queryFn: () => api<Page<Run>>(`/runs?${filters}&page=${page}`),
    refetchInterval: 5000,
  });
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">EXPORT REPORTING</span>
          <h1>Run history</h1>
          <p>Every outcome, every file, and the details behind it.</p>
        </div>
        <a className="button" href={`/api/v1/reports/export.csv?${filters}`}>
          <ArrowDownToLine size={17} />
          Export CSV
        </a>
      </div>
      <ErrorBox error={runs.error} />
      <section className="panel">
        <div className="toolbar history-filters">
          <SearchBox
            value={search}
            onChange={setSearch}
            placeholder="Search this page"
          />
          <select
            aria-label="Filter by status"
            value={state}
            onChange={(e) => {
              setState(e.target.value);
              setPage(1);
            }}
          >
            <option value="">All statuses</option>
            {Object.entries(labels).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
          <select
            aria-label="Filter by task"
            value={taskId}
            onChange={(e) => {
              setTaskId(e.target.value);
              setPage(1);
            }}
          >
            <option value="">All tasks</option>
            {tasks.data?.map((task) => (
              <option key={task.id} value={task.id}>
                {task.name}
              </option>
            ))}
          </select>
          <select
            aria-label="Filter by database"
            value={targetId}
            onChange={(e) => {
              setTargetId(e.target.value);
              setPage(1);
            }}
          >
            <option value="">All databases</option>
            {targets.data?.map((target) => (
              <option key={target.id} value={target.id}>
                {target.name}
              </option>
            ))}
          </select>
        </div>
        <RunTable
          runs={(runs.data?.items || []).filter((run) =>
            `${run.snapshot.name} ${schemaLabel(run.snapshot)} ${run.snapshot.target_name}`
              .toLowerCase()
              .includes(search.toLowerCase()),
          )}
          onSelect={setDetail}
        />
        <div className="pagination">
          <span>
            {runs.data?.total || 0} runs · Page {page}
          </span>
          <div>
            <button
              className="button small"
              aria-label="Previous page"
              disabled={page === 1}
              onClick={() => setPage(page - 1)}
            >
              <ChevronLeft size={16} />
            </button>
            <button
              className="button small"
              aria-label="Next page"
              disabled={page * 25 >= (runs.data?.total || 0)}
              onClick={() => setPage(page + 1)}
            >
              <ChevronRight size={16} />
            </button>
          </div>
        </div>
      </section>
      {detail && (
        <RunDetail id={detail} close={() => setDetail(undefined)} user={user} />
      )}
    </>
  );
}

export function RunDetail({
  id,
  close,
  user,
}: {
  id: string;
  close: () => void;
  user: User;
}) {
  const client = useQueryClient();
  const run = useQuery({
    queryKey: ["run", id],
    queryFn: () => api<Run>(`/runs/${id}`),
    refetchInterval: 5000,
  });
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const [reason, setReason] = useState("");
  return (
    <Modal
      title={run.data?.snapshot.name || "Export details"}
      subtitle={run.data?.oracle_job}
      close={close}
    >
      <ErrorBox error={error || run.error} />
      {run.data && (
        <>
          <div className="detail-status">
            <Badge state={run.data.state} />
            {activeStates.includes(run.data.state) &&
              user.role !== "viewer" && (
                <button
                  className="button danger small"
                  disabled={busy || run.data.cancel_requested}
                  onClick={async () => {
                    setBusy(true);
                    try {
                      await api(`/runs/${id}/cancel`, "POST");
                      client.invalidateQueries({ queryKey: ["run", id] });
                    } catch (e) {
                      setError(e);
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  {run.data.cancel_requested
                    ? "Cancellation requested"
                    : "Cancel export"}
                </button>
              )}
          </div>
          {run.data.state === "reconciliation_required" && (
            <div className="notice">
              The worker is verifying the Oracle job outcome. No duplicate
              export will be started.
              {user.role === "admin" && (
                <div className="reconcile">
                  <Field label="Reason for failure acknowledgment">
                    <input
                      value={reason}
                      onChange={(e) => setReason(e.target.value)}
                      placeholder="Describe your investigation (10+ characters)"
                    />
                  </Field>
                  <button
                    className="button small"
                    disabled={busy || reason.length < 10}
                    onClick={async () => {
                      setBusy(true);
                      try {
                        await api(`/runs/${id}/reconcile`, "POST", {
                          outcome: "failed",
                          reason,
                        });
                        client.invalidateQueries({ queryKey: ["run", id] });
                      } catch (e) {
                        setError(e);
                      } finally {
                        setBusy(false);
                      }
                    }}
                  >
                    Verify inactive & acknowledge failure
                  </button>
                </div>
              )}
            </div>
          )}
          <div className="detail-grid">
            <div>
              <small>Database / schema</small>
              <strong>
                {run.data.snapshot.target_name} /{" "}
                {schemaLabel(run.data.snapshot)}
              </strong>
            </div>
            <div>
              <small>Actual dump size</small>
              <strong>{bytes(run.data.size_bytes)}</strong>
            </div>
            <div>
              <small>Started</small>
              <strong>{time(run.data.started_at)}</strong>
            </div>
            <div>
              <small>Finished</small>
              <strong>{time(run.data.finished_at)}</strong>
            </div>
            <div>
              <small>Duration</small>
              <strong>
                {run.data.duration_seconds === null
                  ? "—"
                  : `${Math.round(run.data.duration_seconds)} seconds`}
              </strong>
            </div>
            <div>
              <small>Estimated data progress</small>
              <strong>
                {run.data.progress === null
                  ? "Unavailable"
                  : `${run.data.progress}%`}
              </strong>
            </div>
          </div>
          <h3 className="form-section">Export configuration</h3>
          <div className="config-pills">
            <span>{run.data.snapshot.parallel} workers</span>
            <span>{run.data.snapshot.file_size_gb} GB per file</span>
            <span>{run.data.snapshot.content}</span>
            <span>{run.data.snapshot.directory}</span>
            <span>{run.data.snapshot.retention_days} day retention</span>
          </div>
          <h3 className="form-section">Files</h3>
          {run.data.snapshot.file_timezone && (
            <p>
              Filename timestamps use {run.data.snapshot.file_timezone} queue
              time.
            </p>
          )}
          {run.data.files.map((file) => (
            <div className="file-row" key={file.id}>
              <div>
                <strong className="mono">{file.filename}</strong>
                <small>
                  {file.directory} · {bytes(file.size_bytes)} ·{" "}
                  {file.deleted_at
                    ? `Deleted ${time(file.deleted_at)}`
                    : "Retained"}
                </small>
                {file.cleanup_error && (
                  <small className="error-text">
                    Cleanup: {file.cleanup_error}
                  </small>
                )}
              </div>
              <HardDrive size={18} />
            </div>
          ))}
          {run.data.errors.length > 0 && (
            <>
              <h3 className="form-section">Errors</h3>
              <pre className="log error-log">{run.data.errors.join("\n")}</pre>
            </>
          )}
          <h3 className="form-section">Job messages</h3>
          <pre className="log">
            {run.data.messages.join("\n") || "No messages captured yet."}
          </pre>
        </>
      )}
    </Modal>
  );
}
