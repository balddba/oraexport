import type { TaskInput } from "./types";

export const defaultTask: TaskInput = {
  name: "",
  target_id: "",
  schema_name: "",
  schema_names: [],
  export_mode: "SCHEMA",
  directory: "",
  enabled: true,
  cron: "0 2 * * *",
  timezone: "America/Detroit",
  retention_days: 30,
  parallel: 1,
  file_size_gb: 10,
  content: "ALL",
  consistent_snapshot: false,
};
export const activeStates = [
  "queued",
  "starting",
  "running",
  "reconciliation_required",
];
export const labels: Record<string, string> = {
  succeeded: "Succeeded",
  completed_with_errors: "With errors",
  failed: "Failed",
  cancelled: "Cancelled",
  queued: "Queued",
  starting: "Starting",
  running: "Running",
  reconciliation_required: "Needs reconciliation",
};
