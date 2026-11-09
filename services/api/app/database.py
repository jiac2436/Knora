from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in {"sqlite://", "sqlite:///:memory:"}:
            kwargs["poolclass"] = StaticPool
    return create_engine(url, **kwargs)


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_schema() -> None:
    from . import models  # noqa: F401
    from .migrations import apply_legacy_columns, require_schema

    if settings.environment == "local":
        with engine.connect() as connection:
            require_schema(connection)
            if connection.execute(text("SELECT 1 FROM local_installations WHERE id = 1")).first() is None:
                raise ValueError("本地身份尚未准备")
        return
    # Preserve existing development/PostgreSQL bootstrap; local migrations are explicit.
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        apply_legacy_columns(connection)
        if connection.dialect.name == "sqlite":
            from .local_changes import install_change_triggers
            install_change_triggers(connection)
