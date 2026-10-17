"""Reports HTTP endpoints.

This module provides FastAPI routes for aggregated execution summary metrics,
CSV export of filtered export history with spreadsheet formula-injection protection,
and paginated administrative audit logs.
"""

import csv
import io
import json
from collections.abc import Iterator
from datetime import datetime

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from oraexport.metadata_service import MetadataService
from oraexport.models.audit_event_response import AuditEventResponse
from oraexport.models.cell_scalar import CellScalar
from oraexport.models.run_state import BUSY, TERMINAL
from oraexport.models.summary_report import SummaryReport

from ..dependencies import Admin, Db, Identity, fields

router = APIRouter()


def sanitize_csv_cell(value: CellScalar) -> CellScalar:
    """Sanitize a cell value against spreadsheet formula injection attacks.

    Prepends a single quote to string values whose stripped representation begins
    with formula triggers ('=', '+', '-', '@') to neutralize command execution in Excel.

    Args:
        value (CellScalar): Raw cell value to inspect and sanitize.

    Returns:
        CellScalar: Escaped string or original value safe for spreadsheet consumption.
    """
    # Neutralize dangerous characters (=, +, -, @) that trigger spreadsheet formula/DDE execution
    if isinstance(value.root, str) and value.root.lstrip().startswith(("=", "+", "-", "@")):
        escaped_value = f"'{value.root}"
        return CellScalar(root=escaped_value)
    return value


@router.get(path="/api/v1/reports/summary")
def summary(db: Db, user: Identity) -> SummaryReport:
    """Summarize job states and actual known dump storage.

    Aggregates run counts across busy and terminal states, computes total retained
    dump bytes on Oracle directories, and reports success rates.

    Args:
        db (Db): Active metadata database session dependency.
        user (Identity): Authenticated user requesting summary report.

    Returns:
        SummaryReport: Summary metrics containing counts, sizes, and operational totals.
    """
    # Aggregate run outcomes and retained file storage through metadata queries
    summary_data = MetadataService(db=db).summary()
    return summary_data


@router.get(path="/api/v1/reports/export.csv")
def export_csv(
    db: Db,
    user: Identity,
    state: str | None = None,
    task_id: str | None = None,
    target_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> StreamingResponse:
    """Export filtered history with spreadsheet formula-injection protection.

    Generates a downloadable CSV spreadsheet containing execution history records.
    Every string cell is sanitized against CSV Injection (Formula Injection / CWE-1236)
    so that maliciously crafted schema or task names cannot execute arbitrary commands
    when opened in desktop spreadsheet software like Microsoft Excel or LibreOffice Calc.

    Args:
        db (Db): Active metadata database session dependency.
        user (Identity): Authenticated user requesting export CSV.
        state (str | None): Optional run state filter ('active', 'succeeded', etc.).
        task_id (str | None): Optional task identifier to filter by.
        target_id (str | None): Optional target identifier to filter by.
        since (datetime | None): Optional start timestamp filter.
        until (datetime | None): Optional end timestamp filter.

    Returns:
        Response: Streaming CSV HTTP response with attachment content disposition.
    """
    if state and state not in BUSY | TERMINAL | {"active"}:
        raise ValueError("Unknown run state")

    def rows() -> Iterator[str]:
        """Yield a header and bounded CSV batches while the request session stays open."""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "Run ID",
                "Task",
                "Target",
                "Schema",
                "State",
                "Started UTC",
                "Finished UTC",
                "Duration seconds",
                "Dump bytes",
                "Errors",
            ]
        )
        yield output.getvalue()
        repository = MetadataService(db=db)
        for batch in repository.run_batches(state=state, task_id=task_id, target_id=target_id, since=since, until=until):
            sizes = repository.dump_sizes(run_ids=[run.id for run in batch])
            output.seek(0)
            output.truncate()
            for run in batch:
                snapshot = json.loads(s=run.snapshot)
                schemas = snapshot.get("schema_names") or [snapshot.get("schema_name", "")]
                schema = "Full export" if snapshot.get("export_mode") == "FULL" else ", ".join(schemas)
                duration = (run.finished_at - run.started_at).total_seconds() if run.finished_at and run.started_at else None
                values = [
                    run.id,
                    snapshot["name"],
                    snapshot["target_name"],
                    schema,
                    run.state,
                    run.started_at.isoformat() + "Z" if run.started_at else None,
                    run.finished_at.isoformat() + "Z" if run.finished_at else None,
                    duration,
                    sizes[run.id],
                    "; ".join(json.loads(s=run.errors)),
                ]
                writer.writerow([sanitize_csv_cell(value=CellScalar(root=value)).root for value in values])
            yield output.getvalue()

    return StreamingResponse(
        content=rows(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="oraexport-history.csv"'},
    )


@router.get(path="/api/v1/audit")
def audit_history(
    db: Db,
    user: Admin,
    page: int = Query(default=1, ge=1),
) -> list[AuditEventResponse]:
    """Return recent administrative audit events.

    Queries recorded audit log entries detailing administrative mutations such as
    user management, provisioning changes, and credential updates.

    Args:
        db (Db): Active metadata database session dependency.
        user (Admin): Authenticated administrative caller enforcing RBAC.
        page (int): 1-indexed page number for pagination.

    Returns:
        list[AuditEventResponse]: List of serialized audit event records.
    """
    # Fetch paginated audit entries and project onto allowed audit fields
    audit_records = MetadataService(db=db).audit(page=page)
    audit_fields = "id user_id action resource_id created_at"
    result_list = [AuditEventResponse.model_validate(obj=fields(item=item, names=audit_fields)) for item in audit_records]
    return result_list
