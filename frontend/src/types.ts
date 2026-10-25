export interface User {
  id: string;
  username: string;
  role: "admin" | "operator" | "viewer";
  enabled: boolean;
}
export interface Registration {
  id: string;
  name: string;
  dsn: string;
  kind: string;
  version: string | null;
}
export interface DiscoveredPdb {
  name: string;
  open_mode: string;
  services: { name: string; dsn: string | null }[];
}
export interface Discovery {
  kind: string;
  version: string;
  pdbs: DiscoveredPdb[];
}
export interface TargetPrefill {
  registrationId: string;
  name: string;
  dsn: string;
  pdb?: DiscoveredPdb;
}
export interface Target {
  id: string;
  registration_id: string;
  name: string;
  dsn: string;
  directory: string | null;
  tablespace: string;
  provisioned: boolean;
  export_username?: string | null;
  edition: string | null;
  container_name: string | null;
}
export interface TaskInput {
  name: string;
  target_id: string;
  schema_name: string;
  schema_names: string[];
  export_mode: "SCHEMA" | "FULL";
  directory: string;
  enabled: boolean;
  cron: string;
  timezone: string;
  retention_days: number;
  parallel: number;
  file_size_gb: number;
  content: "ALL" | "DATA_ONLY" | "METADATA_ONLY";
  consistent_snapshot: boolean;
}
export interface Task extends TaskInput {
  id: string;
  next_run_at: string | null;
}
export interface BackupFile {
  id: string;
  directory: string;
  filename: string;
  kind: string;
  size_bytes: number | null;
  deleted_at: string | null;
  cleanup_attempts: number;
  cleanup_error: string | null;
}
export interface Run {
  id: string;
  task_id: string;
  target_id: string;
  state: string;
  oracle_job: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  progress: number | null;
  errors: string[];
  messages: string[];
  files: BackupFile[];
  size_bytes: number | null;
  duration_seconds: number | null;
  cancel_requested: boolean;
  snapshot: TaskInput & { target_name: string; file_timezone?: string };
}
export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}
export interface Catalog {
  kind: string;
  container_name: string;
  schemas: string[];
  directories: { name: string; path: string }[];
  tablespaces: string[];
}
export interface Summary {
  counts: Record<string, number>;
  retained_bytes: number;
  tasks: number;
  targets: number;
}
