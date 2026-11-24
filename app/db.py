"""SQLite helpers.

The service deliberately uses the standard-library ``sqlite3`` module with
explicit transactions so the concurrency rules are visible:

* reads run inside a deferred transaction (a WAL snapshot);
* batch validation and the site-wide cycle check use a read snapshot only;
* the short publish step runs inside ``BEGIN IMMEDIATE``;
* failures roll back everything, leaving the previous published state intact.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS areas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    area_id INTEGER NOT NULL REFERENCES areas(id),
    base_version INTEGER NOT NULL,
    area_version INTEGER NOT NULL,
    site_version INTEGER NOT NULL,
    additions_count INTEGER NOT NULL,
    withdrawals_count INTEGER NOT NULL,
    relation_count INTEGER,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS layers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    area_id INTEGER NOT NULL REFERENCES areas(id),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (area_id, code)
);

CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    area_id INTEGER NOT NULL REFERENCES areas(id),
    record_number TEXT NOT NULL,
    earlier_layer_id INTEGER NOT NULL REFERENCES layers(id),
    later_layer_id INTEGER NOT NULL REFERENCES layers(id),
    created_by_batch_id INTEGER REFERENCES batches(id),
    created_at TEXT NOT NULL,
    withdrawn_at TEXT,
    withdrawal_reason TEXT,
    withdrawn_by_batch_id INTEGER REFERENCES batches(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_records_area_record_number
    ON records(area_id, record_number);
CREATE INDEX IF NOT EXISTS ix_records_area_active
    ON records(area_id, withdrawn_at);
CREATE INDEX IF NOT EXISTS ix_records_earlier_later
    ON records(earlier_layer_id, later_layer_id);
CREATE INDEX IF NOT EXISTS ix_layers_area
    ON layers(area_id);

CREATE TABLE IF NOT EXISTS site_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    published_version INTEGER NOT NULL
);
"""


def connect(db_path: Path | str, busy_timeout_ms: int = 10_000) -> sqlite3.Connection:
    """Open a connection with the pragmas used by every request."""

    conn = sqlite3.connect(
        str(db_path),
        timeout=busy_timeout_ms / 1000.0,
        isolation_level=None,  # we control BEGIN/COMMIT explicitly
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    return conn


def init_db(db_path: Path | str, busy_timeout_ms: int = 10_000) -> None:
    """Create the schema (idempotent) and enable WAL mode."""

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path, busy_timeout_ms)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO site_state (id, published_version) VALUES (1, 0)"
        )
    finally:
        conn.close()


@contextmanager
def read_transaction(
    db_path: Path | str, busy_timeout_ms: int = 10_000
) -> Iterator[sqlite3.Connection]:
    """Yield a connection inside a consistent read snapshot."""

    conn = connect(db_path, busy_timeout_ms)
    try:
        conn.execute("BEGIN")
        yield conn
    finally:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        conn.close()


@contextmanager
def write_transaction(
    db_path: Path | str, busy_timeout_ms: int = 10_000
) -> Iterator[sqlite3.Connection]:
    """Yield a connection holding SQLite's write lock.

    ``BEGIN IMMEDIATE`` acquires the lock up front, so the version and
    snapshot re-checks made by the caller and the writes that follow cannot be
    interleaved with another publisher.  Callers are expected to have done the
    expensive reads and graph analysis beforehand; keep the critical section
    short.  Any exception rolls the whole transaction back.
    """

    conn = connect(db_path, busy_timeout_ms)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
