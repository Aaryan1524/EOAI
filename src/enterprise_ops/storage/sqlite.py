"""SQLite connections and transaction boundaries."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


DEFAULT_BUSY_TIMEOUT = 5.0


def connect(path: str | Path, *, timeout: float = DEFAULT_BUSY_TIMEOUT) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None, timeout=timeout)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    """Initialize or upgrade a database atomically."""
    directory = Path(__file__).with_name("migrations")
    migrations = ("0001_initial.sql", "0002_actions.sql")
    with transaction(connection, write=True):
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > len(migrations):
            raise ValueError("Database schema is newer than this application")
        for name in migrations[version:]:
            statement = ""
            for line in (directory / name).read_text(encoding="utf-8").splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    connection.execute(statement)
                    statement = ""
            if statement.strip():
                raise ValueError(f"Incomplete migration: {name}")


@contextmanager
def transaction(connection: sqlite3.Connection, *, write: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
