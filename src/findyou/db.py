"""SQLite engine policy and packaged Alembic migration entry point."""

from datetime import datetime, timezone
from importlib import import_module, resources
from pathlib import Path
from sqlite3 import sqlite_version
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from findyou.config import Settings
from findyou.errors import AppError

SCHEMA_HEAD = "0004_life_model"


class Base(DeclarativeBase):
    pass


def new_id() -> str:
    return str(uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        url = make_url(settings.effective_database_url)
        if url.get_backend_name() != "sqlite":
            raise AppError("unsupported_database", "Only SQLite databases are supported.")
        if url.database and url.database != ":memory:":
            Path(url.database).resolve().parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 10})
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

        @event.listens_for(self.engine, "connect")
        def configure_connection(connection, _record):
            cursor = connection.cursor()
            try:
                version = cursor.execute("SELECT sqlite_version()").fetchone()[0]
                if settings.sqlite_journal_mode == "WAL" and tuple(map(int, version.split("."))) < (3, 51, 3):
                    raise AppError(
                        "sqlite_version_unsupported",
                        f"WAL requires SQLite >=3.51.3; loaded library is {version}. "
                        "Upgrade the Python SQLite library or explicitly configure DELETE journal mode.",
                        503,
                    )
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=10000")
                mode = cursor.execute(f"PRAGMA journal_mode={settings.sqlite_journal_mode}").fetchone()[0]
                if mode.upper() != settings.sqlite_journal_mode:
                    raise AppError("sqlite_journal_unavailable", "Configured SQLite journal mode is unavailable.", 503)
            finally:
                cursor.close()

    def initialize(self) -> None:
        from findyou.content_models import Owner

        import_module("findyou.ops")
        # resources.files keeps the migration tree inside the installed package/wheel.
        migration_dir = resources.files("findyou").joinpath("migrations")
        config = Config()
        config.set_main_option("script_location", str(migration_dir).replace("%", "%%"))
        try:
            with self.engine.begin() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
            with self.sessions.begin() as session:
                if session.get(Owner, self.settings.owner_id) is None:
                    session.add(Owner(id=self.settings.owner_id, name=self.settings.owner_name))
        except AppError:
            raise
        except SQLAlchemyError as exc:
            raise AppError("database_initialization_failed", "Database initialization failed.", 503) from exc

    def readiness(self) -> dict:
        try:
            with self.engine.connect() as connection:
                version = connection.scalar(text("SELECT sqlite_version()"))
                mode = connection.scalar(text("PRAGMA journal_mode"))
                exists = connection.scalar(text(
                    "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='alembic_version'"
                ))
                revision = connection.scalar(text("SELECT version_num FROM alembic_version")) if exists else None
                return {"ready": revision == SCHEMA_HEAD, "sqlite_version": version,
                        "journal_mode": mode, "schema_revision": revision}
        except (AppError, SQLAlchemyError):
            return {"ready": False, "sqlite_version": sqlite_version,
                    "journal_mode": None, "schema_revision": None}
