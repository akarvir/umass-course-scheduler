from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session

from coursewatch.config import settings


class Base(DeclarativeBase):
    pass


@lru_cache
def engine():
    return create_engine(settings().database_url, pool_pre_ping=True)


def get_db():
    with Session(engine()) as db:
        yield db


def init_db(database_engine=None):
    from coursewatch import models  # noqa: F401
    from coursewatch.migrations import upgrade

    with (database_engine or engine()).begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(731920270)"))
        Base.metadata.create_all(connection)
        upgrade(connection)


if __name__ == "__main__":
    from coursewatch.db import init_db as initialize

    initialize()
    print("Database initialized")
