"""Runs HTTP endpoints.

This module provides FastAPI routes for inspecting export execution history,
retrieving detailed run metrics and file manifests, canceling queued or running jobs,
deleting dump files from Oracle directories while retaining run metadata, and
manually reconciling stranded runs when unrecoverable database outages occur.
"""

from datetime import datetime

from fastapi import APIRouter, Query

from oraexport.exceptions.application_error import ApplicationError
from oraexport.metadata_service import MetadataService
from oraexport.models.paginated_runs_response import PaginatedRunsResponse
from oraexport.models.reconcile_input import ReconcileInput
from oraexport.models.run import Run
from oraexport.models.run_response import RunResponse

from ...oracle_database import OracleDatabase
from ..dependencies import Admin, Db, Identity, Operator, run_view

router = APIRouter()


@router.get(path="/api/v1/runs")
def runs(
    db: Db,
    user: Identity,
    state: str | None = None,
    task_id: str | None = None,
    target_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> PaginatedRunsResponse:
    """Return filtered, paginated execution history.

    Queries export execution runs matching status, target, task, and date range filters,
    calculates total matching record count, and returns the requested page slice.

    Args:
        db (Db): Active metadata database session dependency.
        user (Identity): Authenticated user requesting run history.
        state (str | None): Optional run status filter ('active', 'succeeded', 'failed').
        task_id (str | None): Optional task identifier to match.
        target_id (str | None): Optional target database identifier to match.
        since (datetime | None): Optional lower bound on run creation timestamp.
        until (datetime | None): Optional upper bound on run creation timestamp.
        page (int): 1-indexed page number for pagination.
        page_size (int): Number of run records to return per page.

    Returns:
        PaginatedRunsResponse: Paginated run items, total count, page number, and page size.
    """
    repository = MetadataService(db=db)
    total = repository.run_count(state=state, task_id=task_id, target_id=target_id, since=since, until=until)
    items = repository.runs(
        state=state,
        task_id=task_id,
        target_id=target_id,
        since=since,
        until=until,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    run_items = [run_view(db=db, run=run) for run in items]
    return PaginatedRunsResponse(items=run_items, total=total, page=page, page_size=page_size)


@router.get(path="/api/v1/targets/{target_id}/exports")
def target_exports(
    target_id: str,
    db: Db,
    user: Identity,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> PaginatedRunsResponse:
    """List all tracked exports for one database, including deleted file manifests.

    Verifies target existence and returns paginated execution runs associated
    specifically with the designated target database.

    Args:
        target_id (str): Unique identifier of the target database.
        db (Db): Active metadata database session dependency.
        user (Identity): Authenticated user requesting target export list.
        page (int): 1-indexed page number for pagination.
        page_size (int): Number of run records to return per page.

    Returns:
        PaginatedRunsResponse: Paginated export runs for the specified target.

    Raises:
        HTTPException: If the target does not exist.
    """
    database = OracleDatabase(target_id=target_id, session=db)
    total = database.run_count()
    items = database.runs(offset=(page - 1) * page_size, limit=page_size)
    return PaginatedRunsResponse(
        items=[run_view(db=db, run=run) for run in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.delete(path="/api/v1/runs/{run_id}/files")
def delete_export_files(
    run_id: str,
    db: Db,
    user: Operator,
) -> RunResponse:
    """Delete a terminal export's exact owned files while preserving execution history.

    Connects to the remote Oracle server via UTL_FILE.FREMOVE, deletes only the exact
    dump and log files created by this run, and marks them deleted in metadata while
    retaining the run log and performance metrics.

    Args:
        run_id (str): Unique identifier of the finished export run.
        db (Db): Active metadata database session dependency.
        user (Operator): Authenticated operator caller enforcing RBAC.

    Returns:
        RunResponse: Updated run representation reflecting file deletion state.

    Raises:
        HTTPException: If the run is not in a terminal state or remote deletion fails.
    """
    # Execute physical remote file deletion and update metadata records
    with db.cursor() as cursor:
        cursor.execute("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key", {"record_key": run_id})
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
    if not query_rows:
        raise ApplicationError(status_code=404, detail="Resource not found")
    result = OracleDatabase(target_id=query_rows[0].target_id, session=db).delete_export_files(run_id=run_id, user=user)
    run_data = run_view(db=db, run=result)
    return run_data


@router.get(path="/api/v1/runs/{run_id}")
def run_detail(
    run_id: str,
    db: Db,
    user: Identity,
) -> RunResponse:
    """Return the run snapshot, progress, errors, and exact file manifest.

    Retrieves full execution details for a single export run including snapshot parameters,
    elapsed progress percentages, collected error lines, and attached dump files.

    Args:
        run_id (str): Unique identifier of the export run.
        db (Db): Active metadata database session dependency.
        user (Identity): Authenticated user requesting run details.

    Returns:
        RunResponse: Detailed run dictionary with snapshot and file manifests.

    Raises:
        HTTPException: If the run does not exist.
    """
    # Fetch run entity and project full execution snapshot
    with db.cursor() as cursor:
        cursor.execute("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key", {"record_key": run_id})
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
    if not query_rows:
        raise ApplicationError(status_code=404, detail="Resource not found")
    run_record = OracleDatabase(target_id=query_rows[0].target_id, session=db).run(run_id=run_id)
    run_data = run_view(db=db, run=run_record)
    return run_data


@router.post(path="/api/v1/runs/{run_id}/cancel")
def cancel_run(
    run_id: str,
    db: Db,
    user: Operator,
) -> RunResponse:
    """Cancel queued work immediately or request a monitored Oracle stop.

    If the run is still queued, transitions it immediately to canceled. If active,
    sets cancel_requested flag so the executor or worker issues DBMS_DATAPUMP.STOP_JOB.

    Args:
        run_id (str): Unique identifier of the run to cancel.
        db (Db): Active metadata database session dependency.
        user (Operator): Authenticated operator caller enforcing RBAC.

    Returns:
        RunResponse: Updated run representation with cancel state.

    Raises:
        HTTPException: If the run is already in a terminal state or not found.
    """
    # Initiate immediate cancellation or flag remote job for monitored shutdown
    with db.cursor() as cursor:
        cursor.execute("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key", {"record_key": run_id})
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
    if not query_rows:
        raise ApplicationError(status_code=404, detail="Resource not found")
    result = OracleDatabase(target_id=query_rows[0].target_id, session=db).cancel_export(run_id=run_id, user=user)
    run_data = run_view(db=db, run=result)
    return run_data


@router.post(path="/api/v1/runs/{run_id}/reconcile")
def acknowledge_run(
    run_id: str,
    body: ReconcileInput,
    db: Db,
    user: Admin,
) -> RunResponse:
    """Resolve stranded inactive jobs explicitly without asserting an unproven success.

    Allows an administrator to resolve runs left in an active state after severe server
    crashes or network partitions, recording an explicit terminal outcome and audit reason.

    Args:
        run_id (str): Unique identifier of the stranded run.
        body (ReconcileInput): Reconciliation payload with outcome ('failed'/'canceled') and reason.
        db (Db): Active metadata database session dependency.
        user (Admin): Authenticated administrative caller enforcing RBAC.

    Returns:
        RunResponse: Updated run representation after manual reconciliation.

    Raises:
        HTTPException: If the run is already terminal or outcome is invalid.
    """
    # Explicitly transition stranded job to an administrator-attested terminal outcome
    with db.cursor() as cursor:
        cursor.execute("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key", {"record_key": run_id})
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
    if not query_rows:
        raise ApplicationError(status_code=404, detail="Resource not found")
    result = OracleDatabase(target_id=query_rows[0].target_id, session=db).reconcile_export(run_id=run_id, body=body, user=user)
    run_data = run_view(db=db, run=result)
    return run_data
