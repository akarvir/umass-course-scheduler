"""Transactional, versioned upgrades for existing installations."""

from sqlalchemy import text

MIGRATIONS = {
    1: [
        "ALTER TABLE courses ADD COLUMN IF NOT EXISTS lease_token VARCHAR(64)",
        "ALTER TABLE courses ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ",
        "ALTER TABLE courses ADD COLUMN IF NOT EXISTS last_check_seconds DOUBLE PRECISION",
        "ALTER TABLE courses ADD COLUMN IF NOT EXISTS last_request_count INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_courses_next_check_at ON courses (next_check_at)",
    ],
}


def upgrade(connection):
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
    )
    applied = set(connection.scalars(text("SELECT version FROM schema_migrations")))
    for version, statements in MIGRATIONS.items():
        if version not in applied:
            for statement in statements:
                connection.execute(text(statement))
            connection.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:v)"), {"v": version}
            )
