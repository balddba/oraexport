"""Domain behavior without a database, worker, or HTTP framework."""

import json
from datetime import datetime, timedelta

import pytest

from oraexport.models.export_run import ExportRun
from oraexport.models.job_status import JobStatus
from oraexport.models.run_snapshot import RunSnapshot

CURRENT = datetime(2026, 10, 13, 12)


def test_snapshot_detaches_nested_values_and_preserves_legacy_format():
    """Neither input mutation nor transport mutation can rewrite queued settings.

    Verifies deep-copy immutability of RunSnapshot dictionary options against mutations
    and ensures legacy single-schema JSON structures serialize and deserialize without loss.
    """
    # Create mutable configuration inputs to verify defensive copying in RunSnapshot
    values = {"schema_names": ["HR"], "options": {"parallel": 1}, "file_prefix": "oe_legacy"}
    snapshot = RunSnapshot(values=values)
    # Mutate original caller dictionary to test decoupling
    values["schema_names"].append("FINANCE")
    values["options"]["parallel"] = 8
    # Mutate retrieved options dictionary to test immutability of stored state
    options = snapshot.options()
    options["schema_names"].append("PAYROLL")
    options["options"]["parallel"] = 4
    # Ensure internal snapshot values remain unchanged despite external mutations
    assert snapshot.options() == {
        "schema_names": ["HR"],
        "options": {"parallel": 1},
        "file_prefix": "oe_legacy",
    }
    # Direct dictionary mutation on MappingProxyType must raise TypeError
    with pytest.raises(TypeError):
        snapshot.values["file_prefix"] = "changed"  # ty: ignore[invalid-assignment] -- Verify immutable mapping rejects mutation at runtime.
    # Verify backward-compatible deserialization and serialization round-trip
    legacy = '{"schema_name": "HR", "directory": "DUMPS"}'
    assert json.loads(RunSnapshot.from_json(legacy).to_json()) == json.loads(legacy)


def test_completion_after_cancel_preserves_real_outcome_and_prior_errors():
    """Cancellation intent and 100% progress never override explicit completion evidence.

    Verifies that applying COMPLETED or EXECUTING status updates to an in-flight export run
    preserves real outcomes, error codes, and completion timestamps even when cancellation
    was requested.
    """
    # Start a running export job and signal cancellation intent
    run = ExportRun(state="running")
    run.request_cancel(CURRENT)
    # Cancellation request does not mark run finished until remote job halts or completes
    assert run.state == "running"
    assert run.finished_at is None
    # Remote Data Pump process is executing with recorded warnings or errors
    run.apply_status(JobStatus(state="EXECUTING", progress=100, errors=["ORA-39166"]), CURRENT)
    assert run.state == "running"
    # Remote Data Pump finishes with completion; record errors and completed_with_errors
    run.apply_status(JobStatus(state="COMPLETED", progress=100), CURRENT + timedelta(seconds=1))
    assert run.state == "completed_with_errors"
    assert run.errors == ["ORA-39166"]
    assert run.finished_at == CURRENT + timedelta(seconds=1)
    # Terminal runs reject subsequent cancellation requests
    with pytest.raises(ValueError, match="terminal"):
        run.request_cancel(CURRENT)


def test_queued_cancel_and_unknown_remote_state():
    """Queued work cancels locally; missing completion evidence remains uncertain.

    Confirms that cancelling a queued run marks it immediately as cancelled, whereas
    an UNKNOWN status transition marks an active run as reconciliation_required.
    """
    # Queued jobs have not yet started remotely and can cancel immediately
    run = ExportRun(state="queued")
    run.request_cancel(CURRENT)
    assert run.state == "cancelled"
    assert run.finished_at == CURRENT
    # Active jobs whose remote state is missing or lost enter reconciliation state
    unknown = ExportRun(state="running")
    unknown.apply_status(JobStatus(state="UNKNOWN", progress=100), CURRENT)
    assert unknown.state == "reconciliation_required"
    assert unknown.finished_at is None


@pytest.mark.parametrize(("outcome", "reason"), [("succeeded", "Checked"), ("failed", " ")])
def test_acknowledgment_requires_failure_or_cancel_and_reason(outcome, reason):
    """Manual reconciliation cannot assert success or omit investigation evidence.

    Args:
        outcome (str): Target reconciled terminal outcome (e.g., 'succeeded', 'failed').
        reason (str): Justification note supplied by the administrator.
    """
    # Reconciliation cannot fabricate success or proceed with blank justification
    run = ExportRun(state="reconciliation_required")
    with pytest.raises(ValueError, match="Acknowledgment requires failure/cancellation and a reason"):
        run.acknowledge(outcome, reason, CURRENT)
    assert run.state == "reconciliation_required"


def test_acknowledgment_rejects_active_run_and_resolves_uncertain_run():
    """Only an uncertain run accepts an explicit administrator outcome.

    Verifies that active running jobs reject manual acknowledgment while stranded
    runs in reconciliation_required transition to terminal failure with admin messages.
    """
    # Active runs cannot be acknowledged while still running
    with pytest.raises(ValueError, match="stranded"):
        ExportRun(state="running").acknowledge("failed", "Investigated", CURRENT)
    # Stranded runs accept manual administrative resolution with audit trail
    run = ExportRun(state="reconciliation_required")
    run.acknowledge("failed", "Investigated", CURRENT)
    assert run.state == "failed"
    assert run.finished_at == CURRENT
    assert run.messages == ["Admin acknowledgment: Investigated"]


@pytest.mark.parametrize("model", [JobStatus, ExportRun])
def test_execution_models_validate_inputs_and_assignments(model):
    """Execution models reject malformed values and unknown fields.

    Args:
        model (type): Mutable execution model under test.
    """
    from pydantic import ValidationError

    instance = model(state="running", progress="25")
    assert instance.progress == 25
    with pytest.raises(ValidationError):
        model(state="running", progress="invalid")
    with pytest.raises(ValidationError):
        model(state="running", unexpected=True)
    with pytest.raises(ValidationError):
        instance.progress = "invalid"
    assert instance.progress == 25
    other = model(state="running")
    instance.errors.append("ORA-39166")
    assert other.errors == []
    restored = model.model_validate_json(instance.model_dump_json())
    assert restored == instance


def test_snapshot_pydantic_serialization_preserves_frozen_legacy_settings():
    """Pydantic round trips retain nested legacy settings and immutable fields."""
    from pydantic import ValidationError

    snapshot = RunSnapshot(values={"options": {"parallel": 2}, "schema_names": ["HR"]})
    restored = RunSnapshot.model_validate_json(snapshot.model_dump_json())
    assert restored.options() == snapshot.options()
    with pytest.raises(TypeError):
        restored.values["options"]["parallel"] = 4
    with pytest.raises(ValidationError):
        restored.values = {}
    with pytest.raises(ValidationError):
        RunSnapshot.model_validate(obj={"values": [], "unexpected": True})
    dumped = restored.model_dump()
    dumped["values"]["options"]["parallel"] = 8
    assert restored.options()["options"]["parallel"] == 2
