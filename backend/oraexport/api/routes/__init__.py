"""OraExport API routes package.

This package provides discrete FastAPI route modules partitioning REST endpoints
by domain resource:
- auth: Authentication session management, login, logout, and local user administration.
- registrations: Logical database definitions, encrypted DBA credentials, and topology discovery.
- targets: Connectable PDB and non-CDB export targets, catalog inspection, and DDL provisioning.
- tasks: Export schedule policies, cron specifications, and ad-hoc job submission.
- runs: Execution monitoring, progress metrics, cancellation, manifests, and reconciliation.
- reports: Summary metrics, CSV export generation with formula injection defense, and audit history.
- notifications: Alert transport channel settings, delivery outbox inspection, and test pings.
"""
