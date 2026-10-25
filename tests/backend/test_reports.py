"""Batched CSV generation, size semantics, and streaming regressions."""

import asyncio
import csv
import io

import pytest
from dbapi import execute

from oraexport.api.routes.reports import export_csv
from oraexport.metadata_service import MetadataService
from oraexport.models.backup_file import BackupFile
from oraexport.models.defaults import now
from oraexport.models.user import User
from oraexport.oracle_database import OracleDatabase


def test_csv_streams_batched_files_and_preserves_unknown_sizes(db, task, monkeypatch):
    """Stream multiple CSV batches with one file lookup per batch and escaped names.

    Args:
        db (Connection): Test metadata transaction.
        task (Task): Seeded export policy.
        monkeypatch (pytest.MonkeyPatch): Batch-size injection.
    """
    task.name = '=SUM(1,2)\n"finance"'
    runs = []
    for index in range(5):
        run = OracleDatabase(task.target_id, db).enqueue(task)
        execute(
            db,
            "UPDATE runs SET state = 'succeeded', finished_at = :at WHERE id = :id",
            {"at": now(), "id": run.id},
        )
        file = BackupFile(
            run_id=run.id,
            directory=task.directory,
            filename=f"piece_{index}.dmp",
            kind="dump",
            size_bytes=None if index == 1 else index * 1024,
        )
        execute(
            db,
            "INSERT INTO backup_files (id, run_id, directory, filename, kind, size_bytes, deleted_at, cleanup_attempts, cleanup_error) VALUES (:id, :run_id, :directory, :filename, :kind, :size_bytes, :deleted_at, :cleanup_attempts, :cleanup_error)",
            file.model_dump(),
            clobs=("cleanup_error",),
        )
        db.commit()
        runs.append(run)
    original = MetadataService.run_batches

    def batches(self, state=None, task_id=None, target_id=None, since=None, until=None):
        """Force multiple bounded batches with a small fixture."""
        return original(self=self, state=state, task_id=task_id, target_id=target_id, since=since, until=until, batch_size=2)

    monkeypatch.setattr(MetadataService, "run_batches", batches)
    db.statements.clear()
    response = export_csv(db, User(username="viewer", password_hash="unused"), state="succeeded")
    assert not db.statements

    async def consume():
        """Read chunks as the HTTP streaming transport would."""
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(consume())
    assert len(chunks) == 4
    parsed = list(csv.reader(io.StringIO("".join(chunks))))
    assert len(parsed) == 6
    data = {row[0]: row for row in parsed[1:]}
    assert all(row[1] == "'" + task.name for row in data.values())
    assert data[runs[0].id][8] == "0"
    assert data[runs[1].id][8] == ""
    assert data[runs[4].id][8] == "4096"
    file_queries = [sql for sql, _ in db.statements if "FROM backup_files" in sql]
    run_queries = [sql for sql, _ in db.statements if "FROM runs" in sql]
    assert len(file_queries) == 3
    assert len(run_queries) == 1
    assert "state = :state" in run_queries[0]


def test_report_empty_batches_and_invalid_limits(db):
    """Empty histories avoid file queries and invalid bounds fail early.

    Args:
        db (Connection): Empty metadata transaction.
    """
    repository = MetadataService(db)
    assert list(repository.run_batches()) == []
    assert repository.dump_sizes([]) == {}
    for size in (0, 1001):
        with pytest.raises(ValueError, match="Batch size must be between 1 and 1000"):
            list(repository.run_batches(batch_size=size))
    with pytest.raises(ValueError, match="At most 1000 runs may be measured in one batch"):
        repository.dump_sizes(["run"] * 1001)
