"""SQLite connections and transaction boundaries."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    """Apply the initial migration to an empty database."""
    migration = Path(__file__).with_name("migrations") / "0001_initial.sql"
    if connection.in_transaction:
        raise ValueError("Initialize outside an active transaction")
    try:
        connection.executescript(f"BEGIN IMMEDIATE;\n{migration.read_text(encoding='utf-8')}\nCOMMIT;")
    except BaseException:
        connection.rollback()
        raise


@contextmanager
def transaction(connection: sqlite3.Connection, *, write: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
