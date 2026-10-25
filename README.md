# OraExport

A FastAPI and React application for scheduled Oracle Data Pump schema and full-target exports. Oracle stores the configuration, encrypted credentials, schedules, queue, run history, file manifests, notification outbox, and audit trail. Dumps remain on the database server.

## Screenshots

These screenshots show the current React interface with fictional sample data. They illustrate the workflow; no live Oracle export was run to produce them.

**Overview:** active jobs, recent outcomes, and recorded retained storage.

![OraExport overview with an active export and recent successful runs](docs/screenshots/overview.png)

**Databases:** register a database, discover PDB services, and provision direct export targets.

![Database registration and provisioned Sales PDB export target](docs/screenshots/databases.png)

**Export tasks:** review schedules and retention, edit a task, or queue a manual run.

![Export task list with schedule, retention policy, and Run now action](docs/screenshots/export-tasks.png)

**Task editor:** choose selected schemas or a full export, an Oracle DIRECTORY, and scheduling and export options.

![Export task editor showing multiple selected schemas and scheduling controls](docs/screenshots/task-editor.png)

**Exports:** inspect each target's recorded exports and manage retained dump and log files.

![Database exports with outcomes, retained file counts, recorded sizes, and actions](docs/screenshots/exports.png)

## Project layout

- `backend/`: Python 3.13+, FastAPI routers, database-owned workflows, domain objects, Oracle adapters, durable worker, and direct Oracle SQL migrations.
- `frontend/`: React + TypeScript + Vite, Tailwind CSS, and TanStack Query.
- `tests/`: Backend and frontend suites, shared test fixtures, and manual HTTP checks.
- `compose.yaml`: API, worker, and frontend reverse proxy.

The backend separates HTTP handling, application workflows, domain rules, and Oracle infrastructure. See [the architecture guide](docs/architecture.md) for class responsibilities, dependency injection, and transaction boundaries. Oracle is the only supported application database.

## Deployment

You need Docker Engine with Docker Compose v2, an externally supplied Oracle 19c+ metadata database, network access from the API and worker to target services, existing filesystem-backed Oracle DIRECTORY objects on the target databases, and a TLS reverse proxy. The metadata schema and target databases have separate roles: metadata stores application state; direct PDB/non-CDB targets run Data Pump and hold the files. Oracle Instant Client is not required: python-oracledb uses thin mode.

Run the following commands from the repository root unless a step changes directory. The key-generation example also requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

1. Create a **dedicated metadata schema**, using your preferred DBA process. For example, customize and run:

   ```sql
   CREATE USER oraexport_meta IDENTIFIED BY "<choose a strong password>"
     DEFAULT TABLESPACE users QUOTA 1G ON users;
   GRANT CREATE SESSION, CREATE TABLE TO oraexport_meta;
   ```

2. Install backend dependencies and generate an encryption key:

   ```bash
   cd backend
   uv sync --locked
   uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
   cd ..
   cp .env.example .env
   ```

   Set `METADATA_URL`, `ENCRYPTION_KEY`, and `ALLOWED_ORIGIN` in `.env`. URL-encode the metadata username/password when constructing the Oracle URL, for example `oracle://user:password@host:1521/?service_name=APPMETA` (the `/APPMETA` service-path form also works). Use addresses reachable from inside the containers; `localhost` there refers to the container itself. The origin must exactly match the browser's external HTTPS origin, including any nondefault port, with no trailing slash. Back up the encryption key separately from the metadata database; existing credentials cannot be decrypted without it. Keep `COOKIE_SECURE=true` in production.

3. Build and start the services:

   ```bash
   docker compose up --build -d --wait
   docker compose exec api oraexport bootstrap-admin --username admin
   docker compose ps -a
   ```

   The API applies metadata migrations during startup before accepting requests. The worker and frontend wait for the API health check to pass. Run one API process/instance so migrations cannot overlap; migration failures prevent API startup. Bootstrap prompts for a password of at least 12 characters and confirmation, and works only when there are no accounts. The frontend nginx service listens at `127.0.0.1:8080`; configure your TLS proxy to forward the public origin to it. Only the frontend port is published; the API and worker communicate over the Compose network. Check failures with `docker compose logs api worker frontend`.

4. Sign in, register a database, and click **Test & discover**. Add a direct target service for each selected PDB or non-CDB. For a non-CDB registration, add a target with the same DSN. Discovery lists PDBs and their network services. Use **Add target** beside a discovered PDB to prefill the registration, display name, and a service DSN using the registration’s listener settings. Select another discovered service or edit the DSN as needed; saving verifies the service reaches the selected PDB. If service discovery or DSN suggestion is unavailable, the name and registration are still prefilled and you can enter the DSN manually.

   Administrators can use **Edit** on a registration to change its name or service DSN. Saved DBA credentials are retained unless **Replace DBA credentials** is selected; replacements also update targets that inherit them. Target service DSNs and export credentials stay unchanged. After changing connection details, use **Test & discover** again.

   Administrators can use **Remove** on a database registration or export target, then confirm removal. Remove a registration's targets first. Targets with export tasks or run history cannot be removed. Removal deletes saved connection records and credentials that are no longer used; Oracle databases, accounts, and export files are unchanged.

5. Provision each target: select an existing DIRECTORY and permanent tablespace, review SQL, and apply it. For consistent exports, grant the optional snapshot privileges during provisioning. Then create export tasks: select **Selected schemas** and use the searchable dropdown to choose one or more application schemas, or select **Full export** for all exportable data and metadata in that PDB or non-CDB. The Content option controls data versus metadata. Preview schedules and use **Run now** or let the worker dispatch scheduled exports. Use **Edit** on an export task to change its target, schemas/full-export scope, schedule, and export options. Existing queued and historical runs retain their original settings.

   Open **Exports**, or **Manage exports** beside a database target, to browse all exports recorded by OraExport for that PDB or non-CDB. The list is paginated and includes outcomes, retained/deleted file counts, recorded dump sizes, and file details. Operators and administrators can confirm **Delete** to permanently remove an export's retained dump and log files while keeping its execution history. Manual deletion can remove the latest successful export regardless of retention policy. Pending, active, and unresolved jobs cannot be deleted. Deletion verifies remote inactivity and exact file ownership; partial failures keep per-file errors and can be retried. Files created outside OraExport are not listed or deleted.

6. Configure SMTP and/or an HTTPS webhook in **Settings**, then save. **Send test notification** queues a real notification through the worker. Check delivery status in the same screen. Use the user-management section to add administrators, operators, or viewers; bootstrap cannot add later accounts.

## Upgrading existing installations

The current metadata revision is `20261015_master_cleanup`. Migration supports the legacy revision `7ee62792313d` and the scope revision `20261009_export_scope`; it adds missing export-scope/schema-selection columns and the durable `runs.master_cleanup_pending` flag as needed. Existing tasks, credentials, and run history are preserved. Existing terminal runs are checked once for pending master-table cleanup. Unknown revisions or mismatched table definitions stop migration.

Back up the metadata schema and retain its encryption key before upgrading. Stop the API and worker before upgrading. API startup applies the migration; wait for the API to be ready before starting a standalone worker. The migration resumes safely if Oracle DDL is interrupted. For a source installation, run from the repository root with the configured environment in `backend/.env`:

```bash
cd backend
uv sync --locked
uv run oraexport migrate --dry-run --upgrade  # SQL for the oldest legacy revision
uv run uvicorn oraexport.main:app --host 127.0.0.1 --port 8000
```

`--dry-run` previews a fresh installation; `--dry-run --upgrade` previews the oldest legacy upgrade, not a database-specific migration plan. The actual `migrate` command inspects the schema and applies only required changes. Do not apply the legacy preview blindly to a database already at the scope revision.

For a container deployment, run from the repository root:

```bash
docker compose stop frontend api worker
docker compose build
docker compose up -d --wait --remove-orphans
docker compose ps -a
```

The API migrates before becoming healthy, then Compose starts the worker and frontend. `--remove-orphans` removes the former migration container. Do not run bootstrap again for an existing installation.

## Local development

Install Python 3.13+, uv, and Node.js 22.12+ with npm. Local development also requires a real Oracle metadata schema and reachable target services; there is no SQLite or in-memory application mode. Keep production credentials out of local test fixtures.

From the repository root, prepare the backend environment:

```bash
cp .env.example backend/.env
uv sync --project backend --locked
uv run --project backend python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Edit `backend/.env`: supply the metadata connection URL and generated encryption key, set `COOKIE_SECURE=false`, and set `ALLOWED_ORIGIN=http://localhost:5173`. The backend loads `.env` from its working directory; Compose reads the root `.env`. These are separate files. Run each process in its own terminal:

```bash
cd backend
uv run uvicorn oraexport.main:app --reload --host 127.0.0.1 --port 8000
# Terminal 2, after the API is ready, from the repository root:
cd backend
uv run oraexport bootstrap-admin
uv run oraexport-worker
# Terminal 3, from the repository root:
cd frontend
npm ci
npm run dev
```

Start each terminal at the repository root. Bootstrap is needed only once per metadata schema.

Open [http://localhost:5173](http://localhost:5173). Use that hostname consistently: opening `http://127.0.0.1:5173` requires setting `ALLOWED_ORIGIN` to that exact origin instead. If Vite selects a different port because 5173 is occupied, update the origin to match or free port 5173. Set `ORAEXPORT_DEV_API` in the frontend terminal when the API uses a different address, for example `ORAEXPORT_DEV_API=http://127.0.0.1:8001 npm run dev`. Vite proxies `/api` to FastAPI; the production nginx service does the same. Local API documentation is at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs); Compose does not publish the API port or proxy `/docs`.

### Environment settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `METADATA_URL` | Required | Oracle URL for the dedicated metadata schema. |
| `ENCRYPTION_KEY` | Required | Fernet key used for saved database and notification secrets. |
| `ALLOWED_ORIGIN` | `https://localhost` | Exact browser origin permitted for mutations. |
| `COOKIE_SECURE` | `true` | Require HTTPS for session cookies; use `false` for local HTTP. |
| `SESSION_HOURS` | `12` | Session lifetime, from 1 to 168 hours. |
| `GLOBAL_CONCURRENCY` | `2` | Maximum active exports across targets. |
| `TARGET_CONCURRENCY` | `1` | Maximum active exports per target. |
| `POLL_SECONDS` | `5` | Worker polling interval in seconds. |
| `METADATA_POOL_MAX` | `10` | Maximum metadata sessions per API/worker process. |
| `METADATA_POOL_WAIT_MS` | `5000` | Wait for an available metadata session. |
| `METADATA_CALL_TIMEOUT_MS` | `20000` | Timeout for a metadata database round trip. |

The backend rejects unknown keys in its `.env` file. `ORAEXPORT_DEV_API` belongs in the frontend shell environment, not the backend `.env`.

## Operation and recovery

- A task exports one or more conventional, unquoted application schemas, or performs a full export of one PDB/non-CDB target, subject to its Content option. CDB roots and PDB seeds cannot be export targets. A CDB root registration is used to discover PDBs, which receive independent local export users.
- DBA credentials remain encrypted for future discovery/provisioning. Generated export credentials are separate and encrypted with Fernet. Existing accounts not owned by the application are refused. Provisioning DDL commits independently; the generated credential is saved first so an interrupted operation can be retried with the same account and password.
- Defaults: `parallel=1`, `file_size_gb=10`, both data and metadata, metadata-only compression, 30-day retention, `America/Detroit` timezone, two global active jobs and one active job per target. Parallelism above one requires Enterprise Edition. Consistent exports use the SCN captured at startup and require appropriate privileges and sufficient Oracle undo retention.
- Scheduling uses five-field cron. Daily/weekly presets generate cron expressions. Timestamps are stored in UTC. Nonexistent DST times are skipped; repeated local times run once. An outage is coalesced into one pending run, and schedules advance beyond the current time. Busy tasks skip additional scheduled occurrences. Disabled tasks may still be run manually.
- The worker operates under a renewable metadata lease; the API does not run an in-memory scheduler. Use one worker service on one server. This deployment is not a multi-server HA system.
- Runs persist their unique Oracle job name and configuration before remote startup. Worker restarts reattach instead of repeating `START_JOB`. Connection loss or a vanished job without an explicit completion log is **Needs reconciliation**, reserving the task/target slot.
- New dump/log filenames use UTC queue time, the registered database name, the PDB container name when available, export scope, and a short run ID: `2026-10-12_143025_PRODDB_SALES_HR+PAYROLL_a7c92e_01.dmp` and `2026-10-12_143025_PRODDB_SALES_HR+PAYROLL_a7c92e.log`. Non-CDB names omit the PDB component; full exports use `FULL`. More than two schemas use a summary such as `HR+PAYROLL+3more`, with the complete selection retained in run details. Display names are made filesystem-safe and bounded in length. Run IDs normally use six characters and extend if an existing filename collides. Names are saved when queued; existing exports keep their original filenames and remain recoverable and eligible for retention cleanup.
- The worker checks status and the recorded English-language job log to establish completion. A completed export with Oracle errors is reported separately from success. Progress refers to estimated data progress, not metadata completion. Unknown file sizes remain **Unavailable**, rather than being reported as zero.
- Operators can cancel queued or active exports. Cancellation is confirmed through Oracle state; a job that completed before cancellation remains completed. An admin may acknowledge a stranded job as failed/cancelled using the reconciliation API, after the backend verifies it is absent or stopped and has no attached sessions. The UI offers failure acknowledgment with a required investigation reason. Success is never asserted by manual acknowledgment.
- Confirmed terminal run history is committed before the worker removes its inactive master table. Failed master-table cleanup remains durably pending and is retried during periodic cleanup, independently of file retention, including the latest successful export and exports with deleted files. Logs/dumps remain until retention expiry. Retention uses the current task policy and always preserves the most recent successful run. It deletes only exact recorded filenames within that run's unique namespace and configured directory; partial failures are retried on subsequent cleanup passes. Execution history remains after file deletion.
- For RAC, use shared filesystem DIRECTORY paths or an instance-pinned service so probing, exports, sizing, and cleanup see the same files. Exports use `cluster_ok=0`. ASM paths are excluded.
- Notifications are persisted and delivered at least once, with up to five attempts and exponential backoff. Webhooks receive an `X-OraExport-Delivery` identifier for receiver-side deduplication. Delivery failures do not change export outcomes. SMTP STARTTLS defaults on; webhook secrets and SMTP passwords are encrypted. Leaving secret inputs blank preserves existing secrets.
- Admins manage database credentials, provisioning, accounts, and notification settings. Operators manage tasks and runs; viewers have read access. Local passwords use Argon2id, and session tokens are stored as hashes. HTTP-only SameSite cookies and same-origin mutation checks protect browser requests. No credentials are included in API responses or audit records.

## API highlights

All application endpoints are under `/api/v1`:

- `/auth/login`, `/auth/me`, `/auth/logout`; `/users` and `/users/{id}/disable`.
- `/registrations`, `/registrations/{id}/discover`, `/targets`, `/targets/{id}/catalog`.
- `/targets/{id}/provision/preview` and `/targets/{id}/provision/apply`.
- `/tasks`, `/tasks/{id}`, `/tasks/{id}/run`, `/schedules/preview`.
- `/runs`, `/runs/{id}`, `/runs/{id}/cancel`, `/runs/{id}/reconcile`.
- `/targets/{id}/exports` (paginated database exports), `DELETE /runs/{id}/files` (manual file deletion).
- `/reports/summary`, `/reports/export.csv`, `/notifications/settings`, `/notifications/test`, `/notifications/deliveries`, `/audit`, `/health`.

Run history and CSV accept `state`, `task_id`, `target_id`, `since`, and `until`. History accepts `page` and `page_size` (maximum 100). API date filters accept ISO timestamps; timezone offsets are normalized to UTC. Validation errors contain field locations and messages without echoing request input.

## Verification

All test suites and fixtures live in the root `tests/` folder: `tests/backend/` for pytest, `tests/frontend/` for Vitest, and `tests/health.http` for manual HTTP checks.

Python static type checking uses ty, installed with the backend development dependencies (`uv sync --project backend --group dev`). The root `ty.toml` targets Python 3.13, checks `backend/oraexport/` and `tests/backend/`, and resolves imports from the backend virtual environment and test helpers. Run the commands below from the repository root; `uv run ty check` also works from `backend/`.

```bash
uv run --project backend pytest
uv run --project backend ruff check backend tests/backend
uv run --project backend ruff format --check backend tests/backend
uv run --project backend ty check
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run build
npm --prefix frontend run format:check
```

Unit/API tests use a test-only Oracle statement recorder with a transactional row store and fake remote Oracle connections; no SQLite or alternate database engine is used. The recorder checks the supported metadata SQL subset and basic constraints, but does not simulate real Oracle locks or isolation. Application metadata uses python-oracledb exclusively; schema changes use packaged Oracle SQL (`uv run oraexport migrate --dry-run` previews it). Existing metadata schemas are upgraded in place to revision `20261015_master_cleanup`, preserving data and adding `runs.master_cleanup_pending` (default true so existing terminal runs are checked once). The API applies migrations during startup; start standalone workers only after the API is ready. `uv run oraexport migrate` remains available for manual migration. Live Oracle tests are skipped unless explicitly enabled; they **create and drop uniquely named accounts and tables and remove exact test export files**. Use disposable test databases and dedicated directories.

Set `ORAEXPORT_LIVE_TESTS=1` and, for each fixture you want to enable, supply:

```text
ORAEXPORT_TEST_PDB_DSN, ORAEXPORT_TEST_PDB_USER, ORAEXPORT_TEST_PDB_PASSWORD
ORAEXPORT_TEST_PDB_DIRECTORY, ORAEXPORT_TEST_PDB_TABLESPACE (default USERS)
ORAEXPORT_TEST_NONCDB_DSN, ORAEXPORT_TEST_NONCDB_USER, ORAEXPORT_TEST_NONCDB_PASSWORD
ORAEXPORT_TEST_NONCDB_DIRECTORY, ORAEXPORT_TEST_NONCDB_TABLESPACE (default USERS)
ORAEXPORT_TEST_CDB_DSN, ORAEXPORT_TEST_CDB_USER, ORAEXPORT_TEST_CDB_PASSWORD
```

`ORAEXPORT_TEST_<fixture>_SYSDBA=1` enables SYSDBA if needed. Run `uv run --project backend pytest -m oracle` from the repository root. Tests cover CDB discovery, PDB/non-CDB provisioning, real exports, recovery using a new worker, cancellation, actual file measurement, and retention deletion. Integration fixture DBA accounts need privileges to create/drop users and grant the generated provisioning SQL privileges.

V1 does not include imports/restores, SSH, ASM dump destinations, central file transfer, SSO, or multi-server HA. Schema exports are logical Data Pump artifacts, not a physical database recovery system.

Metadata connections use a bounded pool per API/worker process (default 10 sessions),
with a 5-second acquisition wait, a 10-second TCP connection timeout, and a 20-second
round-trip timeout. Tune `METADATA_POOL_MAX`, `METADATA_POOL_WAIT_MS`, and
`METADATA_CALL_TIMEOUT_MS` for your deployment. Requests return sessions to the pool
after explicitly rolling back remaining work; API shutdown closes the pool.
CSV reports stream batches of 200 runs and query dump sizes once per batch.
Expired sessions clear authenticated query data and return the browser to sign-in;
initial connection failures provide a retry action.

The metadata integration suite uses **real independent Oracle sessions** for migration
upgrades, rollback, competing leases, and simultaneous scheduler/manual enqueueing.
It is separate from the remote export tests. Opt in with `ORAEXPORT_LIVE_TESTS=1` and:

```text
ORAEXPORT_TEST_METADATA_DSN
ORAEXPORT_TEST_METADATA_USER
ORAEXPORT_TEST_METADATA_PASSWORD
ORAEXPORT_TEST_METADATA_TABLESPACE (default USERS)
```

Use a disposable test database. This explicit test DBA must be able to create/drop
users, grant CREATE SESSION and CREATE TABLE, and allocate quota in the selected
tablespace. Each test creates a unique `OE_META_*` schema and drops it in teardown;
production `METADATA_URL` is never used. Run:

```bash
uv run --project backend pytest -m oracle tests/backend/test_metadata_oracle_integration.py
```
