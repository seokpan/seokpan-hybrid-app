from pydantic import Field

from seokpan.connection_settings import DatabaseTargetSettings


class MigrationSettings(DatabaseTargetSettings):
    """Settings consumed only by the approved single Alembic execution."""

    migration_database_url: str = Field(repr=False)
    database_ca_file: str | None = Field(default=None, repr=False)
