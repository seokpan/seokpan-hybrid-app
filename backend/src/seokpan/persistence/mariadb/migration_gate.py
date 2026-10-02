"""Explicit one-shot Alembic execution gate for the MariaDB provider."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO

from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError

from seokpan.connection_contract import DatabaseTarget
from seokpan.persistence.mariadb.connection import (
    DatabaseConfigurationError,
    DatabaseConnectionError,
    configured_database_target,
    database_ssl_context,
    validated_database_url,
)
from seokpan.persistence.mariadb.settings import MigrationSettings

BASELINE_REVISION = "20260901_0001"
EXPECTED_DRIVER = "mysql+asyncmy"
MIGRATION_ACCOUNT = "db_admin"
MUTATING_ACTIONS = frozenset({"stamp-baseline", "upgrade-head"})


class MigrationGateError(ValueError):
    """Raised when a one-shot migration precondition is not satisfied."""


class AlembicRunner(Protocol):
    """Small seam that keeps command selection testable without a database."""

    def current(self, config: Config) -> None: ...

    def stamp_baseline(self, config: Config) -> None: ...

    def upgrade_head(self, config: Config) -> None: ...


class CommandAlembicRunner:
    """Production runner backed by Alembic's public command API."""

    def current(self, config: Config) -> None:
        command.current(config, verbose=True)

    def stamp_baseline(self, config: Config) -> None:
        command.stamp(config, BASELINE_REVISION)

    def upgrade_head(self, config: Config) -> None:
        command.upgrade(config, "head")


@dataclass(frozen=True, slots=True)
class ExpectedTarget:
    host: str
    port: int
    database: str


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seokpan-migration-gate",
        description="Run one explicitly approved Alembic operation against an exact DB target.",
    )
    parser.add_argument(
        "action",
        choices=("current", "stamp-baseline", "upgrade-head"),
        help="current is read-only; the other actions require --execute and --approval-ref.",
    )
    parser.add_argument("--expect-host", required=True)
    parser.add_argument("--expect-port", type=int, default=3306)
    parser.add_argument("--expect-database", required=True)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("alembic.ini"),
        help="Alembic config path; the one-shot runtime must include its migration assets.",
    )
    parser.add_argument(
        "--approval-ref",
        help="Non-secret reference to the separately approved runtime execution record.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Required acknowledgement for a mutating action.",
    )
    return parser


def _validated_url(
    raw_url: str, expected: ExpectedTarget, target: DatabaseTarget | None = None
) -> URL:
    try:
        url = make_url(raw_url)
        actual_port = 3306 if url.port is None else url.port
    except (ValueError, TypeError, SQLAlchemyError):
        raise MigrationGateError("migration URL is malformed") from None
    actual_database = url.database or ""

    if url.drivername != EXPECTED_DRIVER:
        raise MigrationGateError(f"migration URL must use {EXPECTED_DRIVER}")
    if url.username != MIGRATION_ACCOUNT:
        raise MigrationGateError(f"migration URL must use the {MIGRATION_ACCOUNT} account")
    if url.host != expected.host:
        raise MigrationGateError("database host mismatch")
    if actual_port != expected.port:
        raise MigrationGateError("database port mismatch")
    if actual_database != expected.database:
        raise MigrationGateError("database name mismatch")
    try:
        return validated_database_url(raw_url, "db_admin", target)
    except DatabaseConfigurationError as error:
        raise MigrationGateError(str(error)) from None


def _safe_target(url: URL) -> str:
    port = url.port or 3306
    database = (url.database or "").lstrip("/")
    return f"{url.drivername}://{url.host}:{port}/{database}"


def _config(path: Path) -> Config:
    if not path.is_file():
        raise MigrationGateError(f"Alembic config does not exist: {path}")
    return Config(path)


def run(
    argv: list[str],
    *,
    runner: AlembicRunner | None = None,
    stdout: TextIO = sys.stdout,
) -> int:
    args = _parser().parse_args(argv)
    expected = ExpectedTarget(args.expect_host, args.expect_port, args.expect_database)
    # Pydantic Settings supplies this required field from the environment at runtime.
    try:
        settings = MigrationSettings()  # type: ignore[call-arg]
    except ValidationError:
        raise MigrationGateError("migration settings are missing or invalid") from None
    url = _validated_url(
        settings.migration_database_url, expected, configured_database_target(settings)
    )

    if args.action in MUTATING_ACTIONS:
        if not args.execute:
            raise MigrationGateError("mutating action requires --execute")
        if not args.approval_ref or not args.approval_ref.strip():
            raise MigrationGateError("mutating action requires a non-empty --approval-ref")

    config = _config(args.config)
    database_ssl_context(settings.database_ca_file)
    selected_runner = runner or CommandAlembicRunner()
    print(f"action={args.action}", file=stdout)
    print(f"target={_safe_target(url)}", file=stdout)
    print(f"approval_ref={args.approval_ref or 'not-required-read-only'}", file=stdout)

    if args.action == "current":
        selected_runner.current(config)
    elif args.action == "stamp-baseline":
        selected_runner.stamp_baseline(config)
    else:
        selected_runner.upgrade_head(config)
    return 0


def main() -> None:
    try:
        raise SystemExit(run(sys.argv[1:]))
    except (MigrationGateError, DatabaseConfigurationError, DatabaseConnectionError) as error:
        print(f"migration gate refused: {error}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
