from coursewatch.db import init_db
from sqlalchemy import text


def test_upgrade_existing_database_and_repeat(db_engine):
    with db_engine.begin() as conn:
        for column in (
            "lease_token",
            "lease_expires_at",
            "last_check_seconds",
            "last_request_count",
        ):
            conn.execute(text(f"ALTER TABLE courses DROP COLUMN {column}"))
        conn.execute(
            text(
                "INSERT INTO courses (term, subject, catalog_number, state, detail, "
                "next_check_at, failures) VALUES ('2027 Spring', 'COMPSCI', '220', "
                "'pending', 'Waiting', now(), 0)"
            )
        )
    init_db(db_engine)
    init_db(db_engine)
    with db_engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM schema_migrations")) == 1
        assert conn.scalar(text("SELECT catalog_number FROM courses")) == "220"
        assert conn.scalar(text("SELECT lease_token FROM courses")) is None
