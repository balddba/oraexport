"""Single-server worker owning run claiming and coordinating application services.

This module provides the standalone worker daemon process responsible for executing
background tasks:
- Lease acquisition: Coordinates single-worker leadership via the Oracle metadata lease table.
- Heartbeat daemon: Proactively renews lease ownership every 20 seconds in a background thread.
- Scheduler cycle: Advances cron schedules and queues due export runs.
- Execution monitor: Polls active Data Pump jobs, reconciles logs, and records dump sizes.
- Run claiming: Claims queued runs while honoring global and per-target concurrency limits.
- Retention cleanup: Enforces export retention policies, deleting expired files via UTL_FILE.
- Outbox dispatcher: Retries failed notifications with bounded exponential backoff.
"""

import logging
import threading
import time
from collections.abc import Callable
from datetime import timedelta

from oracledb import Connection

from oraexport.exceptions.safe_error import safe_error
from oraexport.metadata_connections import acquire_metadata_connection
from oraexport.metadata_service import MetadataService
from oraexport.models.defaults import now, uid
from oraexport.models.lease import Lease
from oraexport.models.run import Run
from oraexport.oracle_utilities import OracleUtilities
from oraexport.types import OracleConnectionFactory

from .config import get_settings
from .notifications import NotificationService
from .oracle_database import OracleDatabase
from .scheduling import Scheduler

log = logging.getLogger("oraexport.worker")


class Worker:
    """Coordinate composed services under a renewable Oracle metadata lease.

    Orchestrates scheduling, job dispatching, remote Data Pump execution monitoring,
    export file retention cleanup, and alert notification dispatching under a distributed
    database-backed mutex lease.

    Attributes:
        factory (Callable[[], Connection]): Callable returning raw metadata connections.
        owner (str): Unique worker instance identifier used for lease ownership.
        scheduler (Scheduler): Cron schedule evaluator and run enqueuer.
        notifications (NotificationService): Notification outbox delivery processor.
        connection_factory (OracleConnectionFactory): Remote transport for database workflows.
    """

    def __init__(
        self,
        factory: Callable[[], Connection] | None = None,
        connection_factory: OracleConnectionFactory | None = None,
    ) -> None:
        """Inject metadata and remote factories for its composed services.

        Initializes worker dependencies, generating a unique instance ID to ensure
        clean ownership handoffs during failover.

        Args:
            factory (Callable[[], Connection] | None): Optional raw connection factory for testing.
            connection_factory (OracleConnectionFactory | None): Factory for Oracle connections.
        """
        # Retain the optional raw connection factory for tests
        self.factory = factory
        # Generate random unique worker owner identifier
        self.owner = uid()
        # Compose application service coordinators with injected factories
        self.scheduler = Scheduler()
        self.notifications = NotificationService()
        self.connection_factory = connection_factory or OracleUtilities.connect

    def acquire_or_renew_lease(self, db: Connection) -> bool:
        """Acquire or renew the preseeded singleton lease transactionally.

        Acquires an exclusive pessimistic row lock on the singleton lease record.
        If another active worker owns the lease and it has not expired, rolls back
        the transaction and returns False to maintain single-leader invariants.

        Args:
            db (Connection): Active metadata database session for row locking and persistence.

        Returns:
            bool: True if lease was acquired or renewed by this worker, False otherwise.

        Raises:
            RuntimeError: If the preseeded scheduler lease row is missing from database.
        """
        # Lock preseeded singleton lease row with SELECT FOR UPDATE.
        lease = MetadataService(db=db).lease_for_update()
        if lease is None:
            raise RuntimeError("Run oraexport migrate before starting the worker")
        current = now()
        # Reject acquisition if another worker holds an unexpired lease.
        if lease.owner != self.owner and lease.expires_at > current:
            db.rollback()
            return False
        # Claim or extend ownership for a 120-second lease window.
        lease.owner = self.owner
        with db.cursor() as cursor:
            parameters = {"owner": lease.owner, "record_key": lease.name}
            cursor.execute("UPDATE worker_leases SET owner = :owner WHERE name = :record_key", parameters)
        lease_ttl = timedelta(seconds=120)
        lease.expires_at = current + lease_ttl
        with db.cursor() as cursor:
            parameters = {"expires_at": lease.expires_at, "record_key": lease.name}
            cursor.execute(
                ("UPDATE worker_leases SET expires_at = :expires_at WHERE name = :record_key"),
                parameters,
            )
        db.commit()
        return True

    def renew_owned_lease(self, db: Connection) -> None:
        """Renew only this owner's lease without claiming another worker's lease.

        Args:
            db (Connection): Active metadata database session for row locking and lease renewal.

        Returns:
            None: Extends expiration timestamp if the lease is currently held by this owner.
        """
        # Pessimistically lock lease row to safely check current ownership.
        lease = MetadataService(db=db).lease_for_update()
        # Never steal ownership during heartbeat renewal; only renew if still the owner.
        if lease and lease.owner == self.owner:
            lease_ttl = timedelta(seconds=120)
            lease.expires_at = now() + lease_ttl
            with db.cursor() as cursor:
                parameters = {"expires_at": lease.expires_at, "record_key": lease.name}
                cursor.execute(
                    ("UPDATE worker_leases SET expires_at = :expires_at WHERE name = :record_key"),
                    parameters,
                )
            db.commit()

    def database(self, db: Connection, target_id: str) -> OracleDatabase:
        """Bind one database to this cycle's transaction and remote transport.

        Args:
            db (Connection): Caller-owned metadata session.
            target_id (str): OracleDatabase identifier.

        Returns:
            OracleDatabase: The bound database workflow object.
        """
        return OracleDatabase(target_id=target_id, session=db, connection_factory=self.connection_factory)

    def claim_run(self, db: Connection) -> str | None:
        """Claim a queued run while honoring global and target concurrency limits.

        Enforces concurrency gates and transactional durability:
        1. Checks active runs against global concurrency limit.
        2. Acquires row-level locks on pending runs in FIFO order.
        3. Enforces target concurrency ceiling and task-level mutual exclusion.
        4. Handles pre-execution cancellations cleanly.
        5. Atomically commits starting state before connecting to target Oracle database.

        Args:
            db (Connection): Active metadata database session used to query and claim pending runs.

        Returns:
            str | None: Claimed run identifier in starting state, or None if no eligible run found.
        """
        settings = get_settings()
        repo = MetadataService(db=db)
        active = repo.active()
        # Enforce global concurrency ceiling across all targets and tasks.
        if len(active) >= settings.global_concurrency:
            return None
        # Lock pending runs with SELECT FOR UPDATE to prevent dual-worker claims.
        pending = repo.pending_for_update()
        for run in pending:
            # Check per-target concurrency limit.
            target_active = sum(a.target_id == run.target_id for a in active)
            if target_active >= settings.target_concurrency:
                continue
            # Ensure task mutual exclusion (never run overlapping exports for the same task).
            task_active = any(a.task_id == run.task_id for a in active)
            if task_active:
                continue
            # Process cancellations requested while the run was waiting in queue.
            if run.cancel_requested:
                run.state = "cancelled"
                run.finished_at = now()
                with db.cursor() as cursor:
                    parameters = {
                        "state": run.state,
                        "finished_at": run.finished_at,
                        "record_key": run.id,
                    }
                    cursor.execute(
                        ("UPDATE runs SET state = :state, finished_at = :finished_at WHERE id = :record_key"),
                        parameters,
                    )
                continue
            # Claim run and commit immediately: durability boundary before contacting Oracle.
            run.state = "starting"
            run.started_at = now()
            with db.cursor() as cursor:
                parameters = {
                    "state": run.state,
                    "started_at": run.started_at,
                    "record_key": run.id,
                }
                cursor.execute(
                    ("UPDATE runs SET state = :state, started_at = :started_at WHERE id = :record_key"),
                    parameters,
                )
            db.commit()
            return run.id
        db.commit()
        return None

    def cleanup(self, db: Connection) -> None:
        """Visit databases for retention while maintaining global worker leadership.

        Args:
            db (Connection): Metadata session for the worker cycle.
        """
        target_ids = MetadataService(db=db).target_ids()
        for target_id in target_ids:
            if not self.acquire_or_renew_lease(db=db):
                return
            self.database(db=db, target_id=target_id).cleanup(renew_lease=self.acquire_or_renew_lease)

    def heartbeat(self) -> None:
        """Renew only an already-owned lease while lengthy Oracle operations run.

        Runs as an independent background daemon thread, periodically updating the
        lease expiration timestamp in metadata to prevent lease forfeiture during lengthy
        Data Pump exports or network delays.
        """
        while True:
            time.sleep(20)
            try:
                # Renew existing lease ownership without attempting to steal another worker's lease
                db = self.factory() if self.factory else acquire_metadata_connection()
                try:
                    self.renew_owned_lease(db=db)
                finally:
                    try:
                        db.rollback()
                    finally:
                        db.close()
            except Exception as exc:
                # Scrub potential sensitive connection details from logged exception message
                error_msg = safe_error(exc=exc)
                log.error("Lease heartbeat failed: %s", error_msg)

    def tick(self) -> None:
        """Perform one bounded scheduling, monitoring, cleanup, and outbox cycle.

        Executes a discrete iteration of background worker duties:
        1. Verifies/acquires the distributed singleton scheduler lease lock.
        2. Advances cron task schedules and coalesces missed occurrences.
        3. Monitors active Data Pump jobs, parsing logs and collecting file manifests.
        4. Claims eligible queued runs within global and target concurrency caps.
        5. Performs export retention cleanup if runs finished or 24 hours elapsed.
        6. Processes pending alert notifications in the durable outbox table.
        """
        db = self.factory() if self.factory else acquire_metadata_connection()
        try:
            if not self.acquire_or_renew_lease(db=db):
                return

            # 2. Advance cron schedules and insert queued run records for due tasks
            self.scheduler.schedule(db=db)
            completed = False

            # 3. Monitor and reconcile all currently active export jobs
            active_runs = MetadataService(db=db).active_run_ids()
            for run_id in active_runs:
                # Re-verify lease before each individual remote job probe
                if not self.acquire_or_renew_lease(db=db):
                    return
                with db.cursor() as cursor:
                    cursor.execute(
                        "SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key", {"record_key": run_id}
                    )
                    query_rows = [
                        Run(
                            id=row[0],
                            task_id=row[1],
                            target_id=row[2],
                            occurrence=row[3],
                            snapshot=row[4].read() if hasattr(row[4], "read") else row[4],
                            state=row[5],
                            oracle_job=row[6],
                            created_at=row[7],
                            started_at=row[8],
                            finished_at=row[9],
                            progress=row[10],
                            errors=row[11].read() if hasattr(row[11], "read") else row[11],
                            messages=row[12].read() if hasattr(row[12], "read") else row[12],
                            cancel_requested=row[13],
                            last_seen_at=row[14],
                            notification_created=row[15],
                            master_cleanup_pending=row[16],
                        )
                        for row in cursor.fetchall()
                    ]
                run_finished = self.database(
                    db=db,
                    target_id=query_rows[0].target_id,
                ).monitor_export(run_id=run_id)
                completed = run_finished or completed

            # 4. Dispatch new queued work if concurrency limits permit
            if self.acquire_or_renew_lease(db=db):
                claimed_run_id = self.claim_run(db=db)
                if claimed_run_id:
                    with db.cursor() as cursor:
                        cursor.execute(
                            "SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key",
                            {"record_key": claimed_run_id},
                        )
                        query_rows = [
                            Run(
                                id=row[0],
                                task_id=row[1],
                                target_id=row[2],
                                occurrence=row[3],
                                snapshot=row[4].read() if hasattr(row[4], "read") else row[4],
                                state=row[5],
                                oracle_job=row[6],
                                created_at=row[7],
                                started_at=row[8],
                                finished_at=row[9],
                                progress=row[10],
                                errors=row[11].read() if hasattr(row[11], "read") else row[11],
                                messages=row[12].read() if hasattr(row[12], "read") else row[12],
                                cancel_requested=row[13],
                                last_seen_at=row[14],
                                notification_created=row[15],
                                master_cleanup_pending=row[16],
                            )
                            for row in cursor.fetchall()
                        ]
                    self.database(
                        db=db,
                        target_id=query_rows[0].target_id,
                    ).start_export(run_id=claimed_run_id)

            # 5. Execute export file retention cleanup if jobs completed or daily interval elapsed
            with db.cursor() as cursor:
                cursor.execute(
                    "SELECT name, owner, expires_at, last_cleanup_at FROM worker_leases WHERE name = :record_key",
                    {"record_key": "scheduler"},
                )
                rows = [Lease(name=row[0], owner=row[1], expires_at=row[2], last_cleanup_at=row[3]) for row in cursor.fetchall()]
            lease = rows[0] if rows else None
            if lease is None:
                raise ValueError("Required lease record is missing")
            cleanup_cutoff = now() - timedelta(days=1)
            is_stale_cleanup = not lease.last_cleanup_at or lease.last_cleanup_at < cleanup_cutoff
            should_run_cleanup = completed or is_stale_cleanup
            if should_run_cleanup:
                self.cleanup(db=db)
                lease.last_cleanup_at = now()
                with db.cursor() as cursor:
                    parameters = {
                        "last_cleanup_at": lease.last_cleanup_at,
                        "record_key": lease.name,
                    }
                    cursor.execute(
                        ("UPDATE worker_leases SET last_cleanup_at = :last_cleanup_at WHERE name = :record_key"),
                        parameters,
                    )
                db.commit()

            # 6. Dispatch due notification deliveries from durable outbox
            if self.acquire_or_renew_lease(db=db):
                self.notifications.process_outbox(db=db)
        finally:
            try:
                db.rollback()
            finally:
                db.close()


def main() -> None:
    """Run the durable worker until the process is stopped.

    Initializes logging, constructs the Worker orchestrator, launches the background
    lease heartbeat thread, and enters an infinite polling loop executing tick cycles.
    """
    logging.basicConfig(level=logging.INFO)
    worker = Worker()
    # Launch background lease renewal thread as a daemon to avoid blocking process shutdown
    heartbeat_thread = threading.Thread(target=worker.heartbeat, daemon=True, name="lease-heartbeat")
    heartbeat_thread.start()
    # Main worker polling loop executing scheduled duties at configured poll intervals
    while True:
        try:
            worker.tick()
        except Exception as exc:
            # Mask potential credentials and log sanitized exception string
            error_msg = safe_error(exc=exc)
            log.error("Worker cycle failed: %s", error_msg)
        poll_delay = get_settings().poll_seconds
        time.sleep(poll_delay)


if __name__ == "__main__":
    main()
