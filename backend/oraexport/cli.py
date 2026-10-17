"""Administrative bootstrap commands.

This module provides CLI commands for initializing the OraExport system:
- bootstrap-admin: Creates the initial administrator user interactively without
  exposing passwords on the command line or process list.
- migrate: Applies or previews metadata database DDL schema migrations.
"""

import argparse
import getpass

import oracledb

from oraexport.config import get_settings
from oraexport.encryption_serivce import EncryptionSerivce
from oraexport.metadata_service import MetadataService
from oraexport.migrations import (
    migrate_configured_database,
    preview_sql,
)
from oraexport.models.user import User
from oraexport.oracle_utilities import OracleUtilities


def main() -> None:
    """Bootstrap the first administrator without command-line password exposure.

    Parses command-line arguments to execute administrative bootstrap workflows.
    Prompting passwords via standard getpass prevents passwords from being logged
    in shell history files or exposed in system process listings.

    Raises:
        SystemExit: If arguments are invalid, passwords fail validation, or bootstrap
            is attempted when user accounts already exist.
    """
    # Configure command-line parser with available subcommands and flags
    parser = argparse.ArgumentParser(description="OraExport administration")
    parser.add_argument("command", choices=["bootstrap-admin", "migrate"])
    parser.add_argument("--username", default="admin")
    parser.add_argument("--dry-run", action="store_true", help="Preview migration SQL")
    parser.add_argument("--upgrade", action="store_true", help="Preview legacy schema upgrade")
    args = parser.parse_args()

    # Enforce argument dependency invariants
    if args.upgrade and (args.command != "migrate" or not args.dry_run):
        parser.error(message="--upgrade requires migrate --dry-run")

    # Handle metadata migration subcommand
    if args.command == "migrate":
        if args.dry_run:
            migration_preview = preview_sql(upgrade=args.upgrade)
            print(migration_preview, end="")
        else:
            migrate_configured_database()
            print("Metadata schema is current.")
        return

    # Enforce migrate-only flag restrictions
    if args.dry_run:
        parser.error(message="--dry-run is only available for migrate")

    # Interactively prompt for admin password to avoid command-line / ps exposure
    password = getpass.getpass(prompt="Admin password (12+ characters): ")
    if len(password) < 12:
        parser.error(message="Password must contain at least 12 characters")
    confirmed_password = getpass.getpass(prompt="Confirm password: ")
    if password != confirmed_password:
        parser.error(message="Passwords do not match")

    # Enforce bootstrap precondition: initial admin can only be created on an empty database
    parameters = OracleUtilities.connection_parameters(url=get_settings().metadata_url.get_secret_value())
    db = oracledb.connect(user=parameters.user, password=parameters.password, dsn=parameters.dsn, tcp_connect_timeout=parameters.tcp_connect_timeout)
    try:
        user_count = MetadataService(db=db).user_count()
        if user_count:
            parser.error(message="Bootstrap is only available before any accounts exist")
        # Hash password using Argon2id and persist initial administrator
        password_digest = EncryptionSerivce.hash_password(value=password)
        admin_user = User(username=args.username, password_hash=password_digest, role="admin")
        with db.cursor() as cursor:
            parameters = {
                "id": admin_user.id,
                "username": admin_user.username,
                "password_hash": admin_user.password_hash,
                "role": admin_user.role,
                "enabled": int(admin_user.enabled),
            }
            cursor.execute(
                ("INSERT INTO app_users (id, username, password_hash, role, enabled) VALUES (:id, :username, :password_hash, :role, :enabled)"),
                parameters,
            )
        db.commit()
    finally:
        try:
            db.rollback()
        finally:
            db.close()
    print("Administrator created.")


if __name__ == "__main__":
    main()
