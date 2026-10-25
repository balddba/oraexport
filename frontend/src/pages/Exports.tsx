import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, Trash2 } from "lucide-react";
import { api, bytes, schemaLabel, time } from "../api";
import type { Page, Run, Target, User } from "../types";
import { activeStates } from "../constants";
import { Badge, Empty, ErrorBox, Field, Modal } from "../components/ui";
import { RunDetail } from "./History";

export function Exports({
  user,
  initialTargetId = "",
}: {
  user: User;
  initialTargetId?: string;
}) {
  const client = useQueryClient();
  const [selectedTarget, setSelectedTarget] = useState(initialTargetId);
  const [page, setPage] = useState(1);
  const [detail, setDetail] = useState<string>();
  const [deleting, setDeleting] = useState<Run>();
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const targets = useQuery({
    queryKey: ["targets"],
    queryFn: () => api<Target[]>("/targets"),
  });
  const targetId = selectedTarget || targets.data?.[0]?.id || "";
  const exports = useQuery({
    queryKey: ["exports", targetId, page],
    queryFn: () => api<Page<Run>>(`/targets/${targetId}/exports?page=${page}`),
    enabled: !!targetId,
    refetchInterval: 5000,
  });
  async function remove() {
    if (!deleting) return;
    setBusy(true);
    setError(undefined);
    try {
      await api(`/runs/${deleting.id}/files`, "DELETE");
      setDeleting(undefined);
    } catch (e) {
      setError(e);
    } finally {
      for (const key of ["exports", "history", "run", "summary"])
        client.invalidateQueries({ queryKey: [key] });
      setBusy(false);
    }
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">EXPORT STORAGE</span>
          <h1>Exports</h1>
          <p>
            Browse exports for a database and manage their dump and log files.
          </p>
        </div>
      </div>
      <ErrorBox error={targets.error || exports.error} />
      <section className="panel">
        <div className="toolbar">
          <Field label="Database">
            <select
              value={targetId}
              onChange={(e) => {
                setSelectedTarget(e.target.value);
                setPage(1);
              }}
            >
              {!targets.data?.length && (
                <option value="">Select a database</option>
              )}
              {targets.data?.map((target) => (
                <option key={target.id} value={target.id}>
                  {target.name}
                </option>
              ))}
            </select>
          </Field>
        </div>
        {targets.isPending || (targetId && exports.isPending) ? (
          <p className="muted">Loading exports…</p>
        ) : !exports.data?.items.length ? (
          <Empty
            title="No exports"
            description={
              targetId
                ? "This database has no recorded exports yet."
                : "Add an export target in Databases to get started."
            }
          />
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Export / schemas</th>
                  <th>Created</th>
                  <th>Outcome</th>
                  <th>Files</th>
                  <th>Recorded dump size</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {exports.data.items.map((run) => {
                  const retained = run.files.filter((file) => !file.deleted_at);
                  const active = activeStates.includes(run.state);
                  return (
                    <tr key={run.id}>
                      <td>
                        <strong>{run.snapshot.name}</strong>
                        <small>{schemaLabel(run.snapshot)}</small>
                      </td>
                      <td>{time(run.created_at)}</td>
                      <td>
                        <Badge state={run.state} />
                      </td>
                      <td>
                        {retained.length
                          ? `${retained.length} retained / ${run.files.length}`
                          : run.files.length
                            ? "Deleted"
                            : "No files"}
                        {retained.some((file) => file.cleanup_error) && (
                          <small className="error-text">
                            Deletion needs retry
                          </small>
                        )}
                      </td>
                      <td>{bytes(run.size_bytes)}</td>
                      <td>
                        <div className="row-actions">
                          <button
                            className="button small"
                            onClick={() => setDetail(run.id)}
                          >
                            Details
                          </button>
                          {user.role !== "viewer" && (
                            <button
                              className="button small danger"
                              disabled={active || !retained.length}
                              title={
                                active
                                  ? "Finish or cancel the export before deleting its files"
                                  : undefined
                              }
                              aria-label={`Delete export ${run.snapshot.name} ${run.id}`}
                              onClick={() => {
                                setError(undefined);
                                setDeleting(run);
                              }}
                            >
                              <Trash2 size={14} />
                              Delete
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <div className="pagination">
          <span>
            {exports.data?.total || 0} exports · Page {page}
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
              disabled={page * 25 >= (exports.data?.total || 0)}
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
      {deleting && (
        <Modal
          title="Delete export files"
          subtitle={deleting.snapshot.name}
          close={() => {
            if (!busy) setDeleting(undefined);
          }}
        >
          <p>
            Permanently delete all retained dump and log files for this export
            from {deleting.snapshot.target_name}? Run history will remain.
          </p>
          <p>
            This also allows deletion of the latest successful export,
            regardless of retention policy.
          </p>
          {deleting.files
            .filter((file) => !file.deleted_at)
            .map((file) => (
              <div className="file-row" key={file.id}>
                <div>
                  <strong className="mono">{file.filename}</strong>
                  <small>
                    {file.directory} · {bytes(file.size_bytes)}
                  </small>
                </div>
              </div>
            ))}
          <ErrorBox error={error} />
          <div className="modal-actions">
            <button
              className="button"
              disabled={busy}
              onClick={() => setDeleting(undefined)}
            >
              Cancel
            </button>
            <button className="button danger" disabled={busy} onClick={remove}>
              {busy ? "Deleting…" : "Delete files"}
            </button>
          </div>
        </Modal>
      )}
    </>
  );
}
