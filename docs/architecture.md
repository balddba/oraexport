# OraExport architecture

OraExport groups database-specific workflows under a database owner and keeps
system-wide coordination in the worker.
Oracle is the only application database. The React frontend continues to use
function components and hooks, and consumes the same HTTP API.

## Layout and responsibilities

| Package | Responsibility |
| --- | --- |
| `api/routes/` | HTTP parameters, role dependencies, cookies, response serialization, and CSV encoding |
| `api/dependencies.py` | Authentication dependencies and explicit public-field serializers |
| `domain/` | Execution outcomes, immutable snapshots, and log evidence |
| `oracle_database.py` | Oracle database workflows, remote capabilities, and target ownership |
| `application/` | System scheduling and dispatch, and the notification outbox |
| `metadata_service.py` | Explicit metadata SQL, registration and target workflows, and domain-state mapping |
| `models/` | Pydantic command inputs, API responses, and metadata models, one snake case module per model |
| `migrations.py` | Direct Oracle schema migration SQL |
| `encryption_serivce.py` | Credential encryption, Argon2 password verification, and session-token hashing |
| `oracle_utilities.py` | Oracle LOB normalization, service DSN parsing, and authenticated connection scopes |
| `bootstrap.py` | Production connection wiring and overridable API service providers |
| `main.py` | FastAPI composition, browser security middleware, and error translation |
| `worker.py` | Coordinate composed services under the durable worker lease |

Application services have no FastAPI dependency. They return records or workflow
results; routes choose public response fields and hide credentials. Application
errors carry stable failure categories, which `main.py` maps to HTTP responses.

Callers import each implementation directly from its owning package. There are
no legacy module re-exports, free-function service wrappers, or worker forwarding
methods. CLI commands and `oraexport.main:app` remain unchanged. Metadata migrations preserve existing data and upgrade the schema to
`20261015_master_cleanup`, adding the durable `runs.master_cleanup_pending` flag.

## Principal objects

- `OracleDatabase` represents one `Target`: a directly connectable PDB or non-container database. It owns catalog discovery, provisioning, task validation, export queueing and execution, cancellation, reconciliation, and file retention/deletion.
- `OracleDatabase` reads its target record and export history, enforcing ownership for individual run reads. Its `owned_filename()` method validates exact dump-piece and log filenames against the captured run namespace.
- `OracleDatabase` binds a target ID, caller-owned metadata session, remote connection factory, clock, and identifier allocator. Task and export operations verify ownership before making changes or contacting Oracle. Task updates cannot transfer ownership to another database.
- `ExportRun` owns an export's state transitions, cancellation, acknowledgment, and outcome/error accumulation; metadata `Run` and `BackupFile` records retain its durable history and manifest.
- `QueuedRunSnapshot` validates new export settings; `RunSnapshot` reads older formats and returns detached transport options. Existing exports use captured DSNs and credentials after database configuration changes.
- `Scheduler` locks due tasks and delegates queue creation to their database owners, coalescing overdue schedules.
- `MetadataService` owns metadata reads and writes: accounts, registrations, targets, tasks, run history and filters, manifests, retention queries, credentials, audit events, lease lookups, and notification deliveries. It also owns user creation and disablement, login and logout, registration creation, discovery, updates, deletion, and target creation/deletion. API calls bind it to the request connection and an injectable remote connection factory.
- `Worker` acquires and renews its lease, claims queued work with `claim_run()` under global and target concurrency limits, scans active exports, and visits each database for retention cleanup. Claiming commits startup intent before the database starts the export.
- `EncryptionSerivce` provides credential encryption/decryption, application password hashing and verification, and opaque session-token creation and hashing.
- `NotificationService` owns channel configuration, test events, transactional outbox staging, channel routing, and bounded delivery retries for SMTP email and HTTPS webhook notifications.

Database workflows replace the former task, provisioning, run-record, export,
execution, and cleanup service classes. Schema normalization, manifest validation,
and retention eligibility live with their database owner rather than separate
single-consumer value objects. `MetadataService` is shared by database workflows,
application services, the API, and the worker; it also owns due-task locking.

`OracleDatabase` implements account checks, export account provisioning, and export
task privilege validation directly through `account_exists()`,
`provision_export_account()`, and `verify_export_task()`. The `provision_apply()`
workflow coordinates credentials, account DDL, verification, and metadata updates.
File operations also live directly on `OracleDatabase` through `verify_directory()`,
`read_log()`, `file_size()`, and `remove_file()`, using its active Oracle cursor.
Discovery lives directly on `OracleDatabase` through `discover()` and
`discover_catalog()`. Data Pump operations live directly on `OracleDatabase`
through `start_datapump_job()`, `datapump_job_status()`, `stop_datapump_job()`,
`verify_datapump_inactive()`, and `release_datapump_master()`. These operations
execute SQL without committing metadata; export workflows retain transaction
and recovery decisions. `OracleDatabase.recover_from_log()` reconstructs terminal
Data Pump status and dump pieces from recorded log lines without an active connection.
A `database.connected(dsn, credential)` context binds operations to one authenticated driver connection and shared cursor, then clears them
on exit. Access outside that context and nested connections on the same database
raise an error. Workflows call `self.start_datapump_job()` and
`self.account_exists()` inside the context. `metadata_session` is the
caller-owned persistence transaction; `oracle_connection` and `oracle_cursor` are
the temporary remote session. `OracleUtilities.connect()` yields raw driver
connections and handles authentication, timeouts, sanitized errors, and closure.
`OracleUtilities.parse_dsn()` preserves the registered listener and transport settings
when deriving discovered service addresses. `OracleUtilities.lob_lines()` normalizes
driver strings and LOB locators. Registration discovery uses `OracleDatabase` without a target ID before a target
exists. Target-owned workflows require a saved target ID.
The worker delegates target-specific work to `OracleDatabase` objects. Database methods
implement workflows directly rather than forwarding calls to the removed services.

## Transactions and recovery

Request and worker scopes acquire raw `oracledb.Connection` objects. Each metadata operation
executes its own bound SQL, including explicit INSERT, UPDATE, DELETE, and SELECT statements.
Pydantic records are detached values: changing an attribute does not persist it, and reads always
reflect the current transaction. Every operation opens its own driver cursor, binds parameters, reads LOBs, and decodes rows
directly. Workflows explicitly commit or roll back. The HTTP dependency, CLI, and worker each
open raw driver connections and explicitly roll back remaining work and close them in finally
blocks. API and worker sessions come from a bounded process-local pool with acquisition,
TCP connection, and round-trip timeouts. Request transaction ownership remains
explicit. API shutdown closes its pool. Streaming CSV responses keep their request
session alive until the response finishes and read history in bounded batches.

Transactions remain explicit at the boundaries required by recovery:

1. Manual and scheduled enqueueing hold the task row lock while checking for busy runs and saving the snapshot and manifest.
2. Claiming persists the job identity and `starting` state before a remote startup attempt.
3. An uncertain startup or missing completion evidence reserves the slot as `reconciliation_required`.
4. Terminal state, notification outbox records, and the durable master-cleanup intent commit before remote master-table removal. A successful drop clears the intent; periodic cleanup retries terminal pending runs independently of file retention.
5. Generated credentials commit before provisioning DDL, so an interrupted operation can reuse the saved password.
6. File cleanup records successful deletions and sanitized errors, retaining history and allowing partial-failure retries.

Manual deletion and retention share file ownership checks. Their eligibility rules
remain separate: retention preserves the latest successful run; explicit manual
deletion can remove its files after verified remote inactivity.

## Dependency injection and tests

The API obtains request-scoped `MetadataService` objects and a `DatabaseFactory` through FastAPI `Depends` in `bootstrap.py`
and binds each database to the request metadata session. Tests can override that
factory or inject a remote connection factory directly into a `OracleDatabase`. `Worker`
accepts metadata and remote connection factories. Queue creation
and scheduling also support injected clocks and identifier allocation.

Production metadata uses python-oracledb thin mode and rejects non-Oracle URLs.
There is no SQLite driver, alternate metadata backend, or test database engine.
`tests/dbapi.py` is a test-only statement recorder and transactional row store. It
supports the metadata SQL statements used by the application, checks basic declared constraints,
and fails on unsupported statements. It does **not** establish correctness of
Oracle isolation, row locking, DDL, driver types, or query plans. Driver-boundary
unit tests and the explicitly enabled live Oracle tests provide complementary
coverage; live tests still require disposable Oracle databases and directories.

Run the backend checks with:

```bash
uv run --project backend pytest
uv run --project backend ruff check backend tests/backend
uv run --project backend ruff format --check backend tests/backend
uv run --project backend oraexport migrate --dry-run
```

`tests/backend/test_metadata_oracle_integration.py` additionally creates isolated real Oracle metadata schemas to exercise migrations, rollback, concurrent leases, and scheduler/manual enqueue races. It requires explicit test-only DBA credentials and remains opt-in.
