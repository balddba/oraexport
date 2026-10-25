"""Tasks HTTP endpoints.

This module provides FastAPI routes for defining and maintaining recurring export policies,
calculating next execution occurrences, submitting manual ad-hoc export jobs, and
previewing upcoming schedule execution times in UTC and target local timezones.
"""

from datetime import timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter

from oraexport.exceptions.application_error import ApplicationError
from oraexport.metadata_service import MetadataService
from oraexport.models.defaults import now
from oraexport.models.run_response import RunResponse
from oraexport.models.schedule_input import ScheduleInput
from oraexport.models.task import Task
from oraexport.models.task_input import TaskInput
from oraexport.models.task_response import TaskResponse

from ...oracle_database import OracleDatabase
from ..dependencies import Db, Identity, Operator, run_view, task_view

router = APIRouter()


@router.get(path="/api/v1/tasks")
def tasks(db: Db, user: Identity) -> list[TaskResponse]:
    """List export policies.

    Queries all configured export tasks in display order and projects them into
    serialized task views containing schema lists, directory, and schedule settings.

    Args:
        db (Db): Active metadata database session dependency.
        user (Identity): Authenticated user requesting task list.

    Returns:
        list[TaskResponse]: List of serialized export policy dictionaries.
    """
    # Retrieve all configured export tasks and project onto public task schemas
    task_records = MetadataService(db=db).tasks()
    task_list = [task_view(task=item) for item in task_records]
    return task_list


@router.post(path="/api/v1/tasks", status_code=201)
def create_task(
    body: TaskInput,
    db: Db,
    user: Operator,
) -> TaskResponse:
    """Save a verified export task and calculate its next occurrence.

    Validates schema existences and directory write privileges on the target,
    computes the initial next_run_at timestamp using croniter, and saves the task.

    Args:
        body (TaskInput): Export task configuration payload.
        db (Db): Active metadata database session dependency.
        user (Operator): Authenticated operator caller enforcing RBAC.

    Returns:
        TaskResponse: Serialized task view dictionary for the created task.

    Raises:
        HTTPException: If target validation fails, cron expression is invalid, or name duplicates.
    """
    # Validate target capabilities and persist task with calculated next occurrence
    result = OracleDatabase(target_id=body.target_id, session=db).create_task(body=body, user=user)
    created_view = task_view(task=result)
    return created_view


@router.put(path="/api/v1/tasks/{task_id}")
def update_task(
    task_id: str,
    body: TaskInput,
    db: Db,
    user: Operator,
) -> TaskResponse:
    """Update future runs; existing runs retain their immutable settings snapshot.

    Updates task schedule, schemas, or directory options for future scheduled runs.
    Any previously completed or queued runs preserve their original immutable snapshot.

    Args:
        task_id (str): Unique identifier of the task to update.
        body (TaskInput): Validated update payload.
        db (Db): Active metadata database session dependency.
        user (Operator): Authenticated operator caller enforcing RBAC.

    Returns:
        TaskResponse: Serialized task view dictionary for the updated task.

    Raises:
        HTTPException: If the task does not exist or target options are invalid.
    """
    # Apply updated schedule parameters and recalculate upcoming occurrence
    with db.cursor() as cursor:
        cursor.execute("SELECT id, name, target_id, schema_name, directory, enabled, cron, timezone, retention_days, parallel, file_size_gb, content, consistent_snapshot, next_run_at, export_mode, schema_names FROM tasks WHERE id = :record_key", {"record_key": task_id})
        query_rows = [
            Task(
                id=row[0],
                name=row[1],
                target_id=row[2],
                schema_name=row[3],
                directory=row[4],
                enabled=row[5],
                cron=row[6],
                timezone=row[7],
                retention_days=row[8],
                parallel=row[9],
                file_size_gb=row[10],
                content=row[11],
                consistent_snapshot=row[12],
                next_run_at=row[13],
                export_mode=row[14],
                schema_names=row[15].read() if hasattr(row[15], "read") else row[15],
            )
            for row in cursor.fetchall()
        ]
    if not query_rows:
        raise ApplicationError(status_code=404, detail="Resource not found")
    result = OracleDatabase(target_id=query_rows[0].target_id, session=db).update_task(task_id=task_id, body=body, user=user)
    updated_view = task_view(task=result)
    return updated_view


@router.post(path="/api/v1/tasks/{task_id}/run", status_code=202)
def run_task(
    task_id: str,
    db: Db,
    user: Operator,
) -> RunResponse:
    """Queue a manual export under the same task lock used by the scheduler.

    Acquires a row lock on the task record, validates concurrency constraints,
    creates an immutable run record in 'queued' state, and returns the run snapshot.

    Args:
        task_id (str): Unique identifier of the task to execute manually.
        db (Db): Active metadata database session dependency.
        user (Operator): Authenticated operator caller enforcing RBAC.

    Returns:
        RunResponse: Serialized run view dictionary for the queued execution.

    Raises:
        HTTPException: If another execution is already active for this task or target.
    """
    # Enqueue execution run within a row-locked transaction
    with db.cursor() as cursor:
        cursor.execute("SELECT id, name, target_id, schema_name, directory, enabled, cron, timezone, retention_days, parallel, file_size_gb, content, consistent_snapshot, next_run_at, export_mode, schema_names FROM tasks WHERE id = :record_key", {"record_key": task_id})
        query_rows = [
            Task(
                id=row[0],
                name=row[1],
                target_id=row[2],
                schema_name=row[3],
                directory=row[4],
                enabled=row[5],
                cron=row[6],
                timezone=row[7],
                retention_days=row[8],
                parallel=row[9],
                file_size_gb=row[10],
                content=row[11],
                consistent_snapshot=row[12],
                next_run_at=row[13],
                export_mode=row[14],
                schema_names=row[15].read() if hasattr(row[15], "read") else row[15],
            )
            for row in cursor.fetchall()
        ]
    if not query_rows:
        raise ApplicationError(status_code=404, detail="Resource not found")
    result = OracleDatabase(target_id=query_rows[0].target_id, session=db).queue_export(task_id=task_id, user=user)
    run_data = run_view(db=db, run=result)
    return run_data


@router.post(path="/api/v1/schedules/preview")
def schedule_preview(
    body: ScheduleInput,
    user: Identity,
) -> list[dict[str, str]]:
    """Preview five upcoming UTC and local execution times.

    Computes the next five occurrences of a 5-field cron expression within a designated
    IANA timezone, taking into account daylight saving time transitions.

    Args:
        body (ScheduleInput): Schedule input with cron expression and IANA timezone name.
        user (Identity): Authenticated user requesting the schedule preview.

    Returns:
        list[dict[str, str]]: List of dicts with ISO-formatted UTC and local timestamps.

    Raises:
        ValueError: If the cron expression or timezone is malformed.
    """
    # Start occurrence walk from current UTC timestamp
    current = now()
    times = []
    # Decompose preview calculation into single-line statements <= 100 chars
    tz_obj = ZoneInfo(key=body.timezone)
    for _ in range(5):
        current = OracleDatabase.next_occurrence(expression=body.cron, zone=body.timezone, after=current)
        utc_str = current.isoformat() + "Z"
        local_dt = current.replace(tzinfo=timezone.utc).astimezone(tz=tz_obj)
        local_str = local_dt.isoformat()
        preview_entry = {"utc": utc_str, "local": local_str}
        times.append(preview_entry)
    return times
