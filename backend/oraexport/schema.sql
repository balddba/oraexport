CREATE TABLE app_users (
    id VARCHAR2(32 CHAR) NOT NULL,
    username VARCHAR2(100 CHAR) NOT NULL,
    password_hash VARCHAR2(255 CHAR) NOT NULL,
    role VARCHAR2(20 CHAR) NOT NULL,
    enabled NUMBER(1) NOT NULL CHECK (enabled IN (0, 1)),
    PRIMARY KEY (id),
    UNIQUE (username)
);

CREATE TABLE credentials (
    id VARCHAR2(32 CHAR) NOT NULL,
    username VARCHAR2(128 CHAR) NOT NULL,
    ciphertext CLOB NOT NULL,
    sysdba NUMBER(1) NOT NULL CHECK (sysdba IN (0, 1)),
    PRIMARY KEY (id)
);

CREATE TABLE notification_deliveries (
    id VARCHAR2(32 CHAR) NOT NULL,
    channel VARCHAR2(20 CHAR) NOT NULL,
    payload CLOB NOT NULL,
    state VARCHAR2(20 CHAR) NOT NULL,
    attempts NUMBER(10) NOT NULL,
    next_attempt_at TIMESTAMP NOT NULL,
    error CLOB,
    PRIMARY KEY (id)
);

CREATE TABLE notification_settings (
    id NUMBER(10) NOT NULL,
    config CLOB NOT NULL,
    secrets CLOB,
    PRIMARY KEY (id)
);

CREATE TABLE worker_leases (
    name VARCHAR2(100 CHAR) NOT NULL,
    owner VARCHAR2(32 CHAR) NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    last_cleanup_at TIMESTAMP,
    PRIMARY KEY (name)
);

CREATE TABLE audit_events (
    id VARCHAR2(32 CHAR) NOT NULL,
    user_id VARCHAR2(32 CHAR),
    action VARCHAR2(100 CHAR) NOT NULL,
    resource_id VARCHAR2(100 CHAR) NOT NULL,
    created_at TIMESTAMP NOT NULL,
    FOREIGN KEY (user_id) REFERENCES app_users (id),
    PRIMARY KEY (id)
);

CREATE TABLE login_sessions (
    id VARCHAR2(64 CHAR) NOT NULL,
    user_id VARCHAR2(32 CHAR) NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    FOREIGN KEY (user_id) REFERENCES app_users (id),
    PRIMARY KEY (id)
);

CREATE TABLE registrations (
    id VARCHAR2(32 CHAR) NOT NULL,
    name VARCHAR2(100 CHAR) NOT NULL,
    dsn VARCHAR2(1000 CHAR) NOT NULL,
    credential_id VARCHAR2(32 CHAR) NOT NULL,
    kind VARCHAR2(20 CHAR) NOT NULL,
    version VARCHAR2(100 CHAR),
    FOREIGN KEY (credential_id) REFERENCES credentials (id),
    PRIMARY KEY (id),
    UNIQUE (name)
);

CREATE TABLE targets (
    id VARCHAR2(32 CHAR) NOT NULL,
    registration_id VARCHAR2(32 CHAR) NOT NULL,
    name VARCHAR2(100 CHAR) NOT NULL,
    dsn VARCHAR2(1000 CHAR) NOT NULL,
    admin_credential_id VARCHAR2(32 CHAR) NOT NULL,
    export_credential_id VARCHAR2(32 CHAR),
    directory VARCHAR2(128 CHAR),
    tablespace VARCHAR2(128 CHAR) NOT NULL,
    provisioned NUMBER(1) NOT NULL CHECK (provisioned IN (0, 1)),
    edition VARCHAR2(200 CHAR),
    container_name VARCHAR2(128 CHAR),
    FOREIGN KEY (admin_credential_id) REFERENCES credentials (id),
    FOREIGN KEY (export_credential_id) REFERENCES credentials (id),
    FOREIGN KEY (registration_id) REFERENCES registrations (id),
    PRIMARY KEY (id)
);

CREATE TABLE tasks (
    id VARCHAR2(32 CHAR) NOT NULL,
    name VARCHAR2(100 CHAR) NOT NULL,
    target_id VARCHAR2(32 CHAR) NOT NULL,
    schema_name VARCHAR2(128 CHAR) NOT NULL,
    directory VARCHAR2(128 CHAR) NOT NULL,
    enabled NUMBER(1) NOT NULL CHECK (enabled IN (0, 1)),
    cron VARCHAR2(100 CHAR) NOT NULL,
    timezone VARCHAR2(100 CHAR) NOT NULL,
    retention_days NUMBER(10) NOT NULL,
    parallel NUMBER(10) NOT NULL,
    file_size_gb NUMBER(10) NOT NULL,
    content VARCHAR2(20 CHAR) NOT NULL,
    consistent_snapshot NUMBER(1) NOT NULL CHECK (consistent_snapshot IN (0, 1)),
    next_run_at TIMESTAMP,
    export_mode VARCHAR2(6 CHAR) DEFAULT 'SCHEMA' NOT NULL CHECK (export_mode IN ('SCHEMA', 'FULL')),
    schema_names CLOB DEFAULT '[]' NOT NULL,
    FOREIGN KEY (target_id) REFERENCES targets (id),
    PRIMARY KEY (id)
);

CREATE INDEX ix_tasks_next_run_at ON tasks (next_run_at);

CREATE TABLE runs (
    id VARCHAR2(32 CHAR) NOT NULL,
    task_id VARCHAR2(32 CHAR) NOT NULL,
    target_id VARCHAR2(32 CHAR) NOT NULL,
    occurrence VARCHAR2(100 CHAR) NOT NULL,
    snapshot CLOB NOT NULL,
    state VARCHAR2(40 CHAR) NOT NULL,
    oracle_job VARCHAR2(30 CHAR) NOT NULL,
    created_at TIMESTAMP NOT NULL,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    progress NUMBER(10),
    errors CLOB NOT NULL,
    messages CLOB NOT NULL,
    cancel_requested NUMBER(1) NOT NULL CHECK (cancel_requested IN (0, 1)),
    last_seen_at TIMESTAMP,
    notification_created NUMBER(1) NOT NULL CHECK (notification_created IN (0, 1)),
    master_cleanup_pending NUMBER(1) DEFAULT 1 NOT NULL CHECK (master_cleanup_pending IN (0, 1)),
    FOREIGN KEY (target_id) REFERENCES targets (id),
    FOREIGN KEY (task_id) REFERENCES tasks (id),
    PRIMARY KEY (id),
    UNIQUE (oracle_job),
    CONSTRAINT uq_run_occurrence UNIQUE (task_id, occurrence)
);

CREATE INDEX ix_runs_state ON runs (state);

CREATE INDEX ix_runs_target_id ON runs (target_id);

CREATE INDEX ix_runs_task_id ON runs (task_id);

CREATE TABLE backup_files (
    id VARCHAR2(32 CHAR) NOT NULL,
    run_id VARCHAR2(32 CHAR) NOT NULL,
    directory VARCHAR2(128 CHAR) NOT NULL,
    filename VARCHAR2(255 CHAR) NOT NULL,
    kind VARCHAR2(10 CHAR) NOT NULL,
    size_bytes NUMBER(19),
    deleted_at TIMESTAMP,
    cleanup_attempts NUMBER(10) NOT NULL,
    cleanup_error CLOB,
    FOREIGN KEY (run_id) REFERENCES runs (id),
    PRIMARY KEY (id),
    CONSTRAINT uq_run_file UNIQUE (run_id, directory, filename)
);

CREATE INDEX ix_backup_files_run_id ON backup_files (run_id);
