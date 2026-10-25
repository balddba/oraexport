import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  ArrowRight,
  CalendarClock,
  Database,
  HardDrive,
  Plus,
  ShieldCheck,
} from "lucide-react";
import { api, bytes, time } from "../api";
import type { Page, Run, Summary, User } from "../types";
import { Badge, ErrorBox, Empty, Stat, RunTable } from "../components/ui";
import { RunDetail } from "./History";

export function Overview({
  navigate,
  user,
}: {
  navigate: (screen: string) => void;
  user: User;
}) {
  const summary = useQuery({
    queryKey: ["summary"],
    queryFn: () => api<Summary>("/reports/summary"),
    refetchInterval: 5000,
  });
  const runs = useQuery({
    queryKey: ["recent-runs"],
    queryFn: () => api<Page<Run>>("/runs?page_size=8"),
    refetchInterval: 5000,
  });
  const active = useQuery({
    queryKey: ["active-runs"],
    queryFn: () => api<Page<Run>>("/runs?state=active&page_size=100"),
    refetchInterval: 5000,
  });
  const [detail, setDetail] = useState<string>();
  const counts = summary.data?.counts || {};
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const succeeded = counts.succeeded || 0;
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">EXPORT OPERATIONS</span>
          <h1>Everything in view.</h1>
          <p>Keep your Data Pump exports organized and on schedule.</p>
        </div>
        {user.role !== "viewer" && (
          <button
            className="button primary"
            onClick={() => navigate("Export tasks")}
          >
            <Plus size={17} />
            Manage export tasks
          </button>
        )}
      </div>
      <ErrorBox error={summary.error || runs.error || active.error} />
      <div className="stats">
        <Stat
          title="Configured tasks"
          value={String(summary.data?.tasks ?? "—")}
          detail={`${summary.data?.targets ?? 0} database targets`}
          icon={<CalendarClock />}
        />
        <Stat
          title="Successful exports"
          value={String(succeeded)}
          detail={
            total
              ? `${Math.round((succeeded / total) * 100)}% of all runs`
              : "Your export history starts here"
          }
          icon={<ShieldCheck />}
        />
        <Stat
          title="Active exports"
          value={String(active.data?.total ?? "—")}
          detail="Updated every 5 seconds"
          icon={<Activity />}
        />
        <Stat
          title="Retained dump storage"
          value={bytes(summary.data?.retained_bytes ?? null)}
          detail="Measured files with known sizes"
          icon={<HardDrive />}
        />
      </div>
      <section className="panel">
        <div className="panel-head">
          <div>
            <h2>
              <span className="live-dot" /> In progress
            </h2>
            <p>A live look at the work happening now.</p>
          </div>
          <span className="subtle-chip">{active.data?.total || 0} active</span>
        </div>
        {active.data?.items.length ? (
          <div className="active-grid">
            {active.data.items.map((run) => (
              <button
                className="active-card"
                key={run.id}
                onClick={() => setDetail(run.id)}
              >
                <div>
                  <span className="target-icon">
                    <Database size={20} />
                  </span>
                  <Badge state={run.state} />
                </div>
                <h3>{run.snapshot.name}</h3>
                <p>
                  {run.snapshot.target_name} <span> / </span>{" "}
                  {run.snapshot.schema_name}
                </p>
                <div className="progress">
                  <span style={{ width: `${run.progress ?? 0}%` }} />
                </div>
                <small>
                  {run.progress === null
                    ? "Waiting for progress information"
                    : `${run.progress}% estimated data progress`}
                  <span>{time(run.started_at)}</span>
                </small>
              </button>
            ))}
          </div>
        ) : (
          <Empty
            title="All quiet for now"
            description="Running exports will appear here automatically."
          />
        )}
      </section>
      <section className="panel">
        <div className="panel-head">
          <div>
            <h2>Recent exports</h2>
            <p>The latest outcomes across your workspace.</p>
          </div>
          <button
            className="text-button"
            onClick={() => navigate("Run history")}
          >
            View all history <ArrowRight size={16} />
          </button>
        </div>
        <RunTable runs={runs.data?.items || []} onSelect={setDetail} />
      </section>
      <div className="info-strip">
        <ShieldCheck size={20} />
        <p>
          <strong>Retention that respects your latest export.</strong> The most
          recent successful export of each task is preserved, even after its
          retention period.
        </p>
      </div>
      {detail && (
        <RunDetail id={detail} close={() => setDetail(undefined)} user={user} />
      )}
    </>
  );
}
