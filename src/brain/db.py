"""Explicit private SQLite bootstrap with atomic, checksum-verified migrations."""

import hashlib
import os
import sqlite3
import stat
from importlib.resources import files
from pathlib import Path

from brain.config import Settings


class DatabaseError(RuntimeError):
    """Database initialization failed; messages contain no submitted content."""


def migration_sql() -> str:
    return files("brain").joinpath("migrations/001_initial.sql").read_text(encoding="utf-8")


def _prepare_storage(data_dir: Path) -> Path:
    if data_dir.is_symlink():
        raise DatabaseError("Data directory must not be a symlink")
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = data_dir.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise DatabaseError("Data directory must be owned by the current user")
    allowed = {"memory.sqlite", "memory.sqlite-wal", "memory.sqlite-shm", "service.lock"}
    if any(p.name not in allowed for p in data_dir.iterdir()):
        raise DatabaseError("Use a dedicated memory data directory")
    for name in allowed:
        path = data_dir / name
        if path.is_symlink():
            raise DatabaseError("SQLite files must not be symlinks")
        if path.exists():
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise DatabaseError("SQLite files must be private regular files")
    data_dir.chmod(0o700)
    database = data_dir / "memory.sqlite"
    fd = os.open(database, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    for name in allowed:
        path = data_dir / name
        if path.exists():
            path.chmod(0o600)
    return database


def _migrate(connection: sqlite3.Connection) -> None:
    sql = migration_sql()
    digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    connection.execute("BEGIN IMMEDIATE")
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise DatabaseError("Unsupported database schema version")
        if version == 1:
            row = connection.execute("SELECT version, sha256 FROM schema_migration").fetchall()
            if row != [(1, digest)]:
                raise DatabaseError("Migration checksum does not match the installed package")
        else:
            if connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone():
                raise DatabaseError("Refusing to adopt an unversioned database")
            # executescript commits an existing transaction. Execute complete statements instead,
            # preserving trigger bodies and the transaction around the whole migration.
            statement = ""
            for line in sql.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    connection.execute(statement)
                    statement = ""
            if statement.strip():
                raise DatabaseError("Incomplete migration statement")
            connection.execute(
                "CREATE TABLE schema_migration (version INTEGER PRIMARY KEY, sha256 TEXT NOT NULL)"
                " STRICT"
            )
            connection.execute("INSERT INTO schema_migration VALUES (?, ?)", (1, digest))
            connection.execute("PRAGMA user_version=1")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def initialize_database(
    data_dir: Path, *, busy_timeout_ms: int = 3000, cache_kib: int = 8192
) -> sqlite3.Connection:
    """Return a configured connection owned by the caller; no models/extensions loaded."""
    _validate_connection_settings(busy_timeout_ms, cache_kib)
    connection: sqlite3.Connection | None = None
    try:
        _prepare_storage(data_dir)
        connection = open_database(data_dir, busy_timeout_ms=busy_timeout_ms, cache_kib=cache_kib)
        if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
            raise DatabaseError("SQLite WAL mode is unavailable")
        _migrate(connection)
        return connection
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        if connection is not None:
            connection.close()
        if isinstance(exc, DatabaseError):
            raise
        raise DatabaseError("Cannot initialize the local database") from exc


def open_database(
    data_dir: Path, *, busy_timeout_ms: int = 3000, cache_kib: int = 8192
) -> sqlite3.Connection:
    """Open a previously initialized private store without reacquiring the migration lock."""
    _validate_connection_settings(busy_timeout_ms, cache_kib)
    path = data_dir / "memory.sqlite"
    if path.is_symlink():
        raise DatabaseError("SQLite files must not be symlinks")
    database = path.absolute().as_uri()
    connection = sqlite3.connect(database + "?mode=rw", uri=True, timeout=busy_timeout_ms / 1000)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA recursive_triggers=ON")
        connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        connection.execute(f"PRAGMA cache_size={-cache_kib}")
        connection.execute("PRAGMA synchronous=FULL")
        return connection
    except BaseException:
        connection.close()
        raise


def _validate_connection_settings(busy_timeout_ms: int, cache_kib: int) -> None:
    if type(busy_timeout_ms) is not int or not 0 <= busy_timeout_ms <= 30000:
        raise DatabaseError("Invalid busy timeout")
    if type(cache_kib) is not int or not 256 <= cache_kib <= 16384:
        raise DatabaseError("Invalid cache size")


def initialize(settings: Settings) -> None:
    connection = initialize_database(
        settings.data_dir, busy_timeout_ms=settings.busy_timeout_ms, cache_kib=settings.cache_kib
    )
    connection.close()
