"""Scheduler operations with explicit durability boundaries."""

from datetime import datetime
from typing import Callable

from oracledb import Connection

from oraexport.metadata_service import MetadataService
from oraexport.models.defaults import now
from oraexport.oracle_database import OracleDatabase


class Scheduler:
    """Own the scheduling portion of durable export processing.

    Polls due export tasks based on timezone-aware cron expressions,
    enqueues export runs for non-busy tasks, and advances task schedule
    timestamps strictly beyond the current time to coalesce missed runs.
    """

    def __init__(
        self,
        clock: Callable[[], datetime] = now,
    ) -> None:
        """Inject the scheduling clock.

        Args:
            clock (Callable[[], datetime]): Timestamp factory function used for schedule evaluation.

        Returns:
            None: Initializes the scheduler instance.
        """
        self.clock = clock

    def schedule(self, db: Connection) -> None:
        """Coalesce missed schedules and advance each task beyond current time.

        Acquires row-level locks on all due tasks (next_run_at <= current).
        For each due task:
        1. Checks whether an export run is already active or queued.
        2. If idle, enqueues a new scheduled run with occurrence tag.
        3. Advances next_run_at to the next future occurrence after current time.
        4. Commits task updates and queued runs atomically in a single transaction.

        Args:
            db (Connection): Active metadata database session for row locking and persistence.

        Returns:
            None: Evaluates due tasks, enqueues pending exports, advances schedules, and commits.
        """
        current = self.clock()
        # Lock due tasks with SELECT FOR UPDATE to prevent dual-worker scheduling races.
        tasks = MetadataService(db=db).due_tasks(current=current)
        for task in tasks:
            run_repo = MetadataService(db=db)
            busy = run_repo.has_busy_task(task_id=task.id)
            # Only enqueue if the task does not already have an active/queued export.
            if not busy:
                assert task.next_run_at is not None
                occurrence = "scheduled:" + task.next_run_at.isoformat()
                database = OracleDatabase(target_id=task.target_id, session=db, clock=self.clock)
                database.enqueue(task=task, occurrence=occurrence)
            # Advance schedule strictly beyond current time to coalesce any missed run intervals.
            task.next_run_at = OracleDatabase.next_occurrence(expression=task.cron, zone=task.timezone, after=current)
            with db.cursor() as cursor:
                parameters = {"next_run_at": task.next_run_at, "record_key": task.id}
                cursor.execute(
                    ("UPDATE tasks SET next_run_at = :next_run_at WHERE id = :record_key"),
                    parameters,
                )
        # Commit task schedule advances and newly enqueued runs atomically.
        db.commit()
