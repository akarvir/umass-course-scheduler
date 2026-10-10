from functools import lru_cache

from sqlalchemy import create_engine
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


def init_db():
    from coursewatch import models  # noqa: F401

    Base.metadata.create_all(engine())


if __name__ == "__main__":
    init_db()
    print("Database initialized")
